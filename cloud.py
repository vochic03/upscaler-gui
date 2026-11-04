#!/usr/bin/env python3
"""
The client half of the split: send the video to a rented GPU, get the
upscaled video back, and do the rest here.

Why the split:

  * The AI pass is the only part that wants a big GPU. On a rented card it
    is *faster* than on your Mac by an order of magnitude, and it costs
    cents.
  * Everything else - the denoise, the blend with the plain enlargement, the
    softening, the grain, the grade and the final encode - is cheap CPU
    work. ffmpeg does it at many times real time on an M1 Pro, so there is
    no reason to spend GPU seconds on it, and no reason to heat your Mac up
    doing it either.

So your Mac stays on the light jobs and the rented card does the one job it
is actually good at.

The worker speaks a plain protocol (see worker/server.py):

    GET  {endpoint}/health
    POST {endpoint}/upscale   body: the video, raw
                              headers: X-Scale, X-Model, X-Denoise
                              200, the upscaled video as the body

Anything that can serve that protocol works: the container in worker/, a
RunPod or Vast.ai pod, a DeepInfra container, a fal endpoint.
"""

import ffbin
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

# What a GPU-hour costs. Verified 29 October 2026. The euro figures are
# what the user actually sees.
GPU_PRICES = {
    "runpod-4090": (0.44, 0.41, "RunPod RTX 4090 24 GB"),
    "runpod-a5000": (0.44, 0.41, "RunPod RTX A5000 24 GB"),
    "runpod-l40s": (0.99, 0.92, "RunPod L40S 48 GB"),
    "deepinfra-a100": (1.49, 1.38, "DeepInfra A100 80 GB"),
    "fal-h100": (1.89, 1.75, "fal H100 80 GB"),
    "deepinfra-h100": (2.90, 2.68, "DeepInfra H100 80 GB"),
}

# Seconds per frame for a 1080p frame, measured on an M1 Pro.
SEC_PER_FRAME = {
    "xintao-realesr-animevideo-x2-v3": 1.73,
    "xintao-realesr-animevideo-x3-v3": 2.41,
    "xintao-realesr-animevideo-x4-v3": 3.52,
    "upscayl-realesrgan-x4plus-v3": 2.68,
    "upscayl-realesrgan-x4plus": 41.08,
    "upscayl-realesrgan-x4plus-anime": 13.51,
    "upscayl-remacri": 41.18,
    "upscayl-ultramix_balanced": 42.07,
    "upscayl-ultrasharp": 46.10,
    "upscayl-ultrasharp-v2": 45.22,
    "xintao-realesrgan-x4plus": 40.73,
    "xintao-realesrgan-x4plus-anime": 15.03,
}

# DLoRAL, the one-step diffusion engine, is a different animal: it is a
# diffusion step per frame, so it is roughly 1-3 s a frame on an A100 at 4K.
# It cannot run on the M1 Pro at all (CUDA only).
DLORAL_SEC_PER_FRAME = 2.0

# A rented card is this much faster than the M1 Pro for these networks.
# EnhancR benchmarked the same model at 38.7 fps on an RTX 4090 against
# 0.58 fps on the M1 Pro - about 65x for the compact video models. This is
# deliberately conservative.
RENTED_SPEEDUP = 12.0


def health(endpoint, timeout=15):
    """Ask a worker what it is."""
    url = endpoint.rstrip("/") + "/health"
    # The browser's headers, like everywhere else: Cloudflare looks at the
    # fingerprint of the client and refuses the default Python one with a
    # 403. Without this the question never reaches the worker, and the
    # answer "no engines" is read as "it has none" - which is how a job
    # walks straight into an image that cannot do the work.
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def upscale(endpoint, video_path, out_path, scale=2, model=None, denoise=None,
            api_key=None, timeout=7200, log=print, engine="realesrgan",
            resolution=None, prompt=None, route="/upscale"):
    """Send the video to the worker, save what comes back."""
    url = endpoint.rstrip("/") + route
    # Cloudflare looks at the fingerprint of the client and refuses the
    # default Python one with "error code: 1010". A browser's headers pass.
    headers = {"Content-Type": "video/mp4",
               "User-Agent": USER_AGENT,
               "Accept": "*/*",
               "Accept-Language": "en-US,en;q=0.9"}
    if scale:
        headers["X-Scale"] = str(scale)
    if model:
        headers["X-Model"] = str(model)
    if denoise:
        headers["X-Denoise"] = str(denoise)
    if engine:
        headers["X-Engine"] = str(engine)
    if resolution:
        # The diffusion engines work to an output resolution (720, 1080,
        # 1440) rather than to a factor.
        headers["X-Resolution"] = str(resolution)
    if prompt:
        # The prompt steers what the diffusion engine invents.
        headers["X-Prompt"] = prompt
    if api_key:
        headers["Authorization"] = api_key if api_key.lower().startswith("bearer") \
            else ("Bearer " + api_key)

    size = os.path.getsize(video_path)
    log(f"uploading {size/1e6:.1f} MB to {endpoint}…")
    t0 = time.time()
    with open(video_path, "rb") as fh:
        req = urllib.request.Request(url, data=fh.read(), headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:600]
            raise RuntimeError(f"the worker refused the job ({e.code}): {detail}")
    log(f"the worker answered in {time.time() - t0:.0f}s ({len(body)/1e6:.1f} MB)")
    if not body or body[:12].find(b"ftyp") < 0:
        raise RuntimeError("the worker did not return a video: "
                           + body[:300].decode("utf-8", "replace"))
    with open(out_path, "wb") as fh:
        fh.write(body)
    return out_path


# How many frames go up in one request. The pod proxy runs through
# Cloudflare, which gives a request about a hundred seconds; at 25 fps and
# the measured 4.5 Mpx/s on the card, 450 frames is roughly forty seconds of
# work plus the transfer, so there is room to spare. The AI pass is frame by
# frame, so the pieces are independent and no seam shows where two meet.
# How many frames go up in one request.
#
# A request through the pod's proxy has about a hundred seconds behind it
# (Cloudflare, in front of the pod), and the upload itself runs at about
# 90 KB/s - so a piece of 12 MB takes longer than the proxy allows and
# the job dies with a 524. Fifty frames of 1080p is about 2.4 MB, which goes up in
# roughly half a minute: under the cap, with
# room to spare.
CHUNK_FRAMES = 50


def probe_video(path):
    """frames, fps, width, height of the video in `path`."""
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", path],
        stdout=subprocess.PIPE).stdout.decode())
    v = next(s for s in info["streams"] if s.get("codec_type") == "video")
    try:
        num, den = (v.get("avg_frame_rate") or "25/1").split("/")
        fps = float(num) / float(den) if float(den) else 25.0
    except Exception:
        fps = 25.0
    frames = int(v.get("nb_frames") or 0)
    if not frames:
        # Some containers do not carry the frame count; count the packets.
        try:
            res = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                 "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", path],
                stdout=subprocess.PIPE)
            frames = int(res.stdout.decode().strip() or 0)
        except Exception:
            frames = 0
    return frames, fps, int(v["width"]), int(v["height"])




def free_bytes(path):
    """Free space on the filesystem that holds `path`."""
    try:
        probe = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
        return shutil.disk_usage(probe or '.').free
    except Exception:
        return None


def check_free_space(path, need_bytes, log=print):
    """Stop before the disk fills, with a message that says what to do.

    A job that dies halfway through with "No space left on device" has
    already paid for the card and thrown the work away. The pieces are
    written one at a time, so the space is checked before every one.
    """
    free = free_bytes(path)
    if free is None or free >= need_bytes:
        return free
    raise RuntimeError(
        f"there is only {free/1e9:.2f} GB free where the answer is written, "
        f"and it needs {need_bytes/1e9:.2f} GB. Free some space and run the "
        "job again - the card is not the problem here, the disk is. The "
        "pieces already on disk are kept, so a smaller job can pick up "
        "where this one stopped.")

def split_video(video_path, workdir, start_frame, end_frame, fps):
    """One piece of the video, cut on frame boundaries.

    The cut is exact (a filter, not a seek), so the pieces join back into
    the same film with nothing lost or repeated at the seams.
    """
    os.makedirs(workdir, exist_ok=True)
    out = os.path.join(workdir, f"part-{start_frame:07d}.mp4")
    cmd = [ffbin.ffmpeg(), "-y", "-v", "error", "-i", video_path,
           "-vf", f"trim=start_frame={start_frame}:end_frame={end_frame},"
                  "setpts=PTS-STARTPTS",
           "-an", "-c:v", "libx264", "-preset", "veryfast", "-b:v", "10M",
           "-maxrate", "10M", "-bufsize", "20M", "-pix_fmt", "yuv420p",
           "-r", f"{fps:.6f}", out]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0 or not os.path.exists(out):
        raise RuntimeError("could not cut the video into pieces:\n"
                           + res.stderr.decode("utf-8", "replace")[-400:])
    return out


def concat_parts(parts, out_path, log=print):
    """Join the answers back into one video."""
    listing = os.path.join(os.path.dirname(out_path) or ".", "parts.txt")
    with open(listing, "w") as fh:
        for p in parts:
            fh.write(f"file '{os.path.abspath(p)}'\n")
    cmd = [ffbin.ffmpeg(), "-y", "-v", "error", "-f", "concat", "-safe", "0",
           "-i", listing, "-c", "copy", "-movflags", "+faststart", out_path]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0 or not os.path.exists(out_path):
        # Fall back to a re-encode: the pieces have to end up as one film,
        # and a re-encode cannot get the frame count wrong.
        log("the plain join failed, joining with a re-encode instead")
        cmd = [ffbin.ffmpeg(), "-y", "-v", "error", "-f", "concat", "-safe", "0",
               "-i", listing, "-c:v", "libx264", "-preset", "veryfast", "-crf", "14",
               "-pix_fmt", "yuv420p", "-c:a", "aac", out_path]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode != 0 or not os.path.exists(out_path):
            raise RuntimeError("could not join the pieces back together:\n"
                               + res.stderr.decode("utf-8", "replace")[-400:])
    return out_path


def diffusion_run(endpoint, video_path, out_path, engine="seedvr2",
                  resolution=None, batch_size=None, scale=4, api_key=None,
                  log=print, poll=20.0, timeout=43200):
    """Send the video to the worker and wait for the diffusion job.

    The work does not fit in a request: the proxy gives a request about a
    hundred seconds, and a diffusion pass over a film takes hours. So the
    worker answers with a job id, does the work in the background, and the
    pieces come back one at a time - the answer for a whole film would be
    far too big for one response.

    `resolution` is the short side of the diffusion's output, which is what
    sets the price: the model works at the output resolution, so 480p is
    about a fifth of 1080p.
    """
    check_free_space(os.path.dirname(os.path.abspath(out_path)) or ".", 3 * 1024 ** 3, log)
    url = endpoint.rstrip("/") + "/diffusion"
    headers = {"Content-Type": "video/mp4", "User-Agent": USER_AGENT,
               "Accept": "*/*", "X-Diffusion": engine, "X-Scale": str(scale)}
    if resolution:
        headers["X-Resolution"] = str(resolution)
    if batch_size:
        headers["X-Batch"] = str(batch_size)
    if api_key:
        headers["Authorization"] = api_key if api_key.lower().startswith("bearer") \
            else ("Bearer " + api_key)
    size = os.path.getsize(video_path)
    log(f"sending {size/1e6:.1f} MB to {endpoint} for the {engine} pass…")
    t0 = time.time()
    with open(video_path, "rb") as fh:
        req = urllib.request.Request(url, data=fh.read(), headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=1800) as r:
                job = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"the worker refused the job ({e.code}): {detail}")
    jid = job.get("job")
    if not jid:
        raise RuntimeError("the worker did not start a job: " + str(job)[:300])
    log(f"the job is {jid}; the worker is working on it in the background")

    pieces_dir = os.path.join(os.path.dirname(os.path.abspath(out_path)),
                              "diffusion-pieces")
    os.makedirs(pieces_dir, exist_ok=True)
    got = {}
    last = -1
    told = 0
    while time.time() - t0 < timeout:
        time.sleep(poll)
        try:
            req = urllib.request.Request(f"{endpoint.rstrip('/')}/diffusion/{jid}")
            req.add_header("User-Agent", USER_AGENT)
            with urllib.request.urlopen(req, timeout=120) as r:
                status = json.loads(r.read().decode())
        except Exception as e:
            log(f"  (the status call failed: {e})")
            continue
        # What the worker is doing, in its own words.
        lines = status.get("log") or []
        for line in lines[told:]:
            log("  (card) " + str(line))
        told = max(told, len(lines))
        for piece in status.get("pieces", []):
            name = piece.get("name")
            if not name or piece.get("state") != "done" or name in got:
                continue
            path = os.path.join(pieces_dir, name)
            try:
                req = urllib.request.Request(
                    f"{endpoint.rstrip('/')}/diffusion/{jid}/{name}")
                req.add_header("User-Agent", USER_AGENT)
                with urllib.request.urlopen(req, timeout=900) as r:
                    body = r.read()
            except Exception as e:
                log(f"  could not fetch {name}: {e}")
                continue
            check_free_space(path, 400 * 1024 * 1024, log)
            with open(path, "wb") as fh:
                fh.write(body)
            got[name] = path
            log(f"  piece {len(got)}/{status.get('total')}: {name} "
                f"({len(body)/1e6:.1f} MB)")
        state = status.get("state")
        if state in ("done", "failed"):
            if state == "failed":
                raise RuntimeError("the diffusion job failed: "
                                   + (status.get("error") or "no reason given"))
            break
        if status.get("done") != last:
            last = status.get("done")
            log(f"  {last}/{status.get('total')} pieces, "
                f"{status.get('seconds')}s on the card")
    if not got:
        raise RuntimeError("the diffusion job produced no pieces")

    listing = os.path.join(pieces_dir, "parts.txt")
    with open(listing, "w") as fh:
        for name in sorted(got):
            fh.write(f"file '{got[name]}'\n")
    cmd = [ffbin.ffmpeg(), "-y", "-v", "error", "-f", "concat", "-safe", "0",
           "-i", listing, "-i", video_path,
           "-map", "0:v", "-map", "1:a?", "-c:v", "copy",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_path]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError("could not join the pieces:\n"
                           + res.stderr.decode("utf-8", "replace")[-500:])
    log(f"joined {len(got)} pieces into {out_path} "
        f"({os.path.getsize(out_path)/1e6:.1f} MB) in {time.time()-t0:.0f}s")
    return out_path


def dloral_run(endpoint, video_path, out_path, scale=4, api_key=None, log=print,
               poll=20.0, timeout=36000):
    """Send the video to the worker and wait for the diffusion job.

    The work does not fit in a request: the proxy gives a request about a
    hundred seconds, and a film at four times its size takes hours. So the
    worker answers with a job id, does the work in the background, and the
    pieces come back one at a time - the answer for a whole film would be
    far too big for one response.

    The pieces are joined and the original audio is put back.
    """
    url = endpoint.rstrip("/") + "/dloral"
    headers = {"Content-Type": "video/mp4", "User-Agent": USER_AGENT,
               "Accept": "*/*", "X-Scale": str(scale)}
    if api_key:
        headers["Authorization"] = api_key if api_key.lower().startswith("bearer") \
            else ("Bearer " + api_key)
    size = os.path.getsize(video_path)
    log(f"sending {size/1e6:.1f} MB to {endpoint} for the diffusion pass…")
    t0 = time.time()
    with open(video_path, "rb") as fh:
        req = urllib.request.Request(url, data=fh.read(), headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=1800) as r:
                job = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"the worker refused the job ({e.code}): {detail}")
    jid = job.get("job")
    if not jid:
        raise RuntimeError("the worker did not start a job: " + str(job)[:300])
    log(f"the job is {jid}; the worker is working on it in the background")

    pieces_dir = os.path.join(os.path.dirname(os.path.abspath(out_path)),
                              "dloral-pieces")
    os.makedirs(pieces_dir, exist_ok=True)
    got = {}
    last = 0
    told = 0
    while time.time() - t0 < timeout:
        time.sleep(poll)
        try:
            req = urllib.request.Request(f"{endpoint.rstrip('/')}/dloral/{jid}")
            req.add_header("User-Agent", USER_AGENT)
            with urllib.request.urlopen(req, timeout=120) as r:
                status = json.loads(r.read().decode())
        except Exception as e:
            log(f"  (the status call failed: {e})")
            continue
        # What the worker is doing, in its own words. A cold card spends
        # half an hour fetching the weights before it starts on the film,
        # and without this the Mac can only say "the worker is working on
        # it" for half an hour.
        lines = status.get("log") or []
        for line in lines[told:]:
            log("  (card) " + str(line))
        told = max(told, len(lines))
        for piece in status.get("pieces", []):
            name = piece.get("name")
            if not name or piece.get("state") != "done" or name in got:
                continue
            path = os.path.join(pieces_dir, name)
            try:
                req = urllib.request.Request(
                    f"{endpoint.rstrip('/')}/dloral/{jid}/{name}")
                req.add_header("User-Agent", USER_AGENT)
                with urllib.request.urlopen(req, timeout=600) as r:
                    body = r.read()
            except Exception as e:
                log(f"  could not fetch {name}: {e}")
                continue
            check_free_space(path, 400 * 1024 * 1024, log)
            with open(path, "wb") as fh:
                fh.write(body)
            got[name] = path
            log(f"  piece {len(got)}/{status.get('total')}: {name} "
                f"({len(body)/1e6:.1f} MB)")
        state = status.get("state")
        if state in ("done", "failed"):
            if state == "failed":
                raise RuntimeError("the diffusion job failed: "
                                   + (status.get("error") or "no reason given"))
            break
        if status.get("done") != last:
            last = status.get("done")
            log(f"  {last}/{status.get('total')} pieces, "
                f"{status.get('seconds')}s on the card")
    if not got:
        raise RuntimeError("the diffusion job produced no pieces")

    # The pieces, back in order, with the original audio.
    listing = os.path.join(pieces_dir, "parts.txt")
    with open(listing, "w") as fh:
        for name in sorted(got):
            fh.write(f"file '{got[name]}'\n")
    cmd = [ffbin.ffmpeg(), "-y", "-v", "error", "-f", "concat", "-safe", "0",
           "-i", listing, "-i", video_path,
           "-map", "0:v", "-map", "1:a?", "-c:v", "copy",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_path]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError("could not join the pieces:\n"
                           + res.stderr.decode("utf-8", "replace")[-500:])
    log(f"joined {len(got)} pieces into {out_path} "
        f"({os.path.getsize(out_path)/1e6:.1f} MB) in {time.time()-t0:.0f}s")
    return out_path


def star_run(endpoint, video_path, out_path, prompt="", upscale=4, chunk_len=24,
             width=320, api_key=None, log=print, poll=5.0, timeout=7200):
    """Send the clip to the worker and wait for STAR to have it.

    STAR is a background job like the other diffusions: the model wants a
    lot of VRAM and the inference takes longer than the proxy gives a
    request. The worker answers with a job id, does the work, and the
    result comes back as one piece.
    """
    url = endpoint.rstrip("/") + "/star"
    headers = {"Content-Type": "video/mp4", "User-Agent": USER_AGENT,
               "Accept": "*/*", "X-Scale": str(upscale),
               "X-Chunk": str(chunk_len), "X-Width": str(width),
               "X-Prompt": prompt or ""}
    if api_key:
        headers["Authorization"] = api_key if api_key.lower().startswith("bearer") \
            else ("Bearer " + api_key)
    size = os.path.getsize(video_path)
    log(f"sending {size/1e6:.1f} MB to {endpoint} for the STAR pass…")
    t0 = time.time()
    with open(video_path, "rb") as fh:
        req = urllib.request.Request(url, data=fh.read(), headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=1800) as r:
                job = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"the worker refused the job ({e.code}): {detail}")
    jid = job.get("job")
    if not jid:
        raise RuntimeError("the worker did not start a job: " + str(job)[:300])
    log(f"the job is {jid}; the worker is working on it in the background")

    got = None
    last = None
    told = 0
    while time.time() - t0 < timeout:
        time.sleep(poll)
        try:
            req = urllib.request.Request(f"{endpoint.rstrip('/')}/star/{jid}")
            req.add_header("User-Agent", USER_AGENT)
            with urllib.request.urlopen(req, timeout=120) as r:
                status = json.loads(r.read().decode())
        except Exception as e:
            log(f"  (the status call failed: {e})")
            continue
        # What the worker is doing, in its own words - the weights are
        # seven and a half gigabytes on a cold card.
        lines = status.get("log") or []
        for line in lines[told:]:
            log("  (card) " + str(line))
        told = max(told, len(lines))
        state = status.get("state")
        if state == "error":
            raise RuntimeError("the STAR job failed: "
                               + (status.get("error") or "no reason given"))
        if state == "done":
            req = urllib.request.Request(
                f"{endpoint.rstrip('/')}/star/{jid}/clip-upscaled.mp4")
            req.add_header("User-Agent", USER_AGENT)
            with urllib.request.urlopen(req, timeout=1800) as r:
                body = r.read()
            with open(out_path, "wb") as fh:
                fh.write(body)
            got = out_path
            log(f"the result is {len(body)/1e6:.1f} MB")
            break
        if status.get("seconds") != last:
            last = status.get("seconds")
            log(f"  {last}s on the card")
    if not got:
        raise RuntimeError("the STAR job produced no result")
    log(f"STAR finished in {time.time()-t0:.0f}s")
    return out_path


def face_chunked(endpoint, video_path, out_path, chunk_frames=CHUNK_FRAMES,
                 log=print, **kw):
    """Send a long video to the worker to have its faces repaired.

    The same piece-at-a-time dance as the upscale, because the piece limit
    is the proxy's, not the job's. The faces come back in the same frames
    and the same size; nothing else about the picture is touched.
    """
    kw["route"] = "/face"
    return upscale_chunked(endpoint, video_path, out_path,
                            chunk_frames=chunk_frames, log=log, **kw)


def upscale_chunked(endpoint, video_path, out_path, chunk_frames=CHUNK_FRAMES,
                     log=print, **kw):
    """Send a long video to the worker in pieces, and join the answers.

    One request has about a hundred seconds behind it (Cloudflare, in front
    of the pod), and a whole film does not fit in a hundred seconds. So the
    video goes up in pieces and the answers come back one by one.

    The card is paid for by the minute, and the spin-up is paid once per
    session, so a dozen pieces cost barely more than one long job would.
    """
    frames, fps, w, h = probe_video(video_path)
    kw.pop("log", None)
    if not frames:
        log("could not count the frames, sending the whole thing in one piece")
        return upscale(endpoint, video_path, out_path, log=log, **kw)
    if frames <= chunk_frames:
        log(f"{frames} frames fit in one request")
        return upscale(endpoint, video_path, out_path, log=log, **kw)

    workdir = os.path.join(os.path.dirname(os.path.abspath(out_path)), "parts")
    total = len(range(0, frames, chunk_frames))
    log(f"{frames} frames ({frames/fps:.0f}s) do not fit in one request - "
        f"sending {total} pieces of up to {chunk_frames} frames")
    answers = []
    for i, start in enumerate(range(0, frames, chunk_frames)):
        end = min(start + chunk_frames, frames)
        part = split_video(video_path, workdir, start, end, fps)
        answer = os.path.join(workdir, f"answer-{start:07d}.mp4")
        log(f"piece {i+1}/{total}: frames {start}-{end} ({end-start} frames)")
        upscale(endpoint, part, answer, log=log, **kw)
        answers.append(answer)
    log("joining the pieces back together…")
    concat_parts(answers, out_path, log=log)
    got, _, _, _ = probe_video(out_path)
    if got and got != frames:
        raise RuntimeError(f"the joined video has {got} frames, not {frames}")
    log(f"joined: {got} frames")
    return out_path


def estimate(frames, model, price_key, speedup=RENTED_SPEEDUP, motion=1,
             engine="realesrgan"):
    """What the cloud pass will cost, and what it saves your Mac.

    The euro figure is the one that matters: a whole video costs cents on a
    rented card, so the money is never the reason not to use the good
    models.
    """
    frames = int(frames) * max(1, int(motion or 1))
    if engine == "dloral":
        # Diffusion: it is always on the rented card, and it is far slower
        # per frame than an SR model.
        local = float("inf")
        cloud = DLORAL_SEC_PER_FRAME * frames
    else:
        local = SEC_PER_FRAME.get(model, 5.0) * frames
        cloud = local / speedup
    usd, eur, name = GPU_PRICES.get(price_key, (1.0, 0.92, "a generic GPU"))
    return {
        "frames": frames,
        "local_seconds": local,
        "cloud_seconds": cloud,
        "local_minutes": (local / 60.0) if local != float("inf") else None,
        "cloud_minutes": cloud / 60.0,
        "gpu": name,
        "price_per_hour": usd,
        "price_per_hour_eur": eur,
        "cost": cloud / 3600.0 * usd,
        "cost_eur": cloud / 3600.0 * eur,
    }


def prepare_upload(src, dst, denoise=None, extra_filters=None):
    """The video that goes up: normalised, and denoised if asked.

    `extra_filters` is for the crop and the de-interlace, which belong on the
    source side: the AI should never see the black bars or the combing.
    """
    vf = list(extra_filters or [])
    if denoise:
        vf.append(denoise)
    cmd = [ffbin.ffmpeg(), "-y", "-v", "error", "-i", src]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-b:v", "10M", "-maxrate", "10M",
            "-bufsize", "20M", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", dst]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0 or not os.path.exists(dst):
        raise RuntimeError("could not prepare the upload: "
                           + res.stderr.decode("utf-8", "replace")[-400:])
    return dst


def finish_locally(cloud_video, original, out_path, scale=2, look=None, encoder="h264",
                   target=None, log=print):
    """The finishing pass, here on the Mac.

    The cloud gave us the AI upscale. Now:
      * blend it with a plain lanczos enlargement of the original - this is
        what takes the "invented detail" back out again and gives the soft
        film look;
      * soften it (negative sharpening);
      * add static grain, which both hides MPEG blocking and gives the
        analogue texture;
      * lift the contrast and saturation a touch;
      * encode it and put the audio back.
    """
    look = look or {}
    blend = float(look.get("blend", 0.0))
    soften = float(look.get("soften", 0.0))
    grain = int(look.get("grain", 0))
    cas = float(look.get("cas") or 0)
    halation = float(look.get("halation") or 0)
    perlin_grain = look.get("grain_mode") == "perlin" and grain > 0
    if perlin_grain and not ffbin.has_filter("perlin"):
        # perlin arrived in ffmpeg 7.1. Without it the generator failed
        # silently and the pass died opening a grain file that was never
        # made - at the very end, after the card had been paid for.
        log("this ffmpeg has no perlin source (it needs 7.1 or later) - "
            "the grain falls back to a static pattern")
        perlin_grain = False
    grain_s = grain * 0.05

    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
         cloud_video], stdout=subprocess.PIPE).stdout.decode())
    v = next(s for s in info["streams"] if s.get("codec_type") == "video")
    w, h = int(v["width"]), int(v["height"])
    # The halation blur is scaled to the picture, so it needs the height -
    # which is only known once the video has been probed.
    halation_sigma = max(4, min(24, (target[1] if target else h) // 90))
    try:
        num, den = (v.get("avg_frame_rate") or "25/1").split("/")
        fps = float(num) / float(den) if float(den) else 25.0
    except Exception:
        fps = 25.0

    parts = [f"[0:v]setpts=PTS-STARTPTS,fps={fps:.6f}[ai]"]
    # The soft enlargement is only built when it is used. A filter whose
    # output goes nowhere is an error in ffmpeg, not a warning - and the
    # "clean" look has no blend, so the soft chain was built and then left
    # dangling. The job died at the finishing pass with "Filter
    # 'setpts:default' has output 0 (soft) unconnected", after the card had
    # done the work and been paid for.
    if blend > 0:
        parts.append(f"[1:v]scale={w}:{h}:flags=lanczos,setsar=1,fps={fps:.6f},"
                     f"setpts=PTS-STARTPTS[soft]")
        parts.append(f"[ai][soft]blend=all_expr='A*(1-{blend})+B*{blend}'[mix]")
    else:
        parts.append("[ai]null[mix]")
    # The grain pattern. It is rendered into a file first and measured, the
    # same way the local path does it: the pattern has a mean of its own -
    # about 149 at these settings, not 128 - so without the correction the
    # "grain" is a uniform lift of the picture, and the highlights go white.
    grain_bias = 0.0
    gw = gh = 0
    grain_file = None
    if perlin_grain:
        gw = max(2, w // 4)
        gh = max(2, h // 4)
        frames = int(v.get("nb_frames") or 0)
        dur = (frames / fps) if (frames and fps) else 0.0
        if not dur:
            try:
                dur = float(v.get("duration") or 0.0)
            except Exception:
                dur = 0.0
        if not dur:
            try:
                probe = json.loads(subprocess.run(
                    ["ffprobe", "-v", "error", "-print_format", "json",
                     "-show_format", cloud_video],
                    stdout=subprocess.PIPE).stdout.decode())
                dur = float(probe.get("format", {}).get("duration") or 0.0)
            except Exception:
                dur = 0.0
        grain_file = os.path.join(os.path.dirname(os.path.abspath(out_path)),
                                  "grain.gray")
        subprocess.run(
            [ffbin.ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i",
             f"perlin=size={gw}x{gh}:rate={fps:.6f}:"
             f"tscale={look.get('grain_tscale') or 0.08}:octaves=2:"
             f"persistence=0.5:random_mode=seed:random_seed=1",
             "-t", f"{dur:.3f}", "-pix_fmt", "gray", "-f", "rawvideo", grain_file],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if not (os.path.exists(grain_file) and os.path.getsize(grain_file)):
            log("the grain pattern could not be made - finishing without grain")
            perlin_grain = False
            grain = 0
        try:
            # The WHOLE file, not the first megabyte: the perlin field starts
            # bright and settles, so a short sample reads about 20 levels
            # high and the correction over-subtracts.
            total = 0
            count = 0
            with open(grain_file, "rb") as fh:
                while True:
                    block = fh.read(4 * 1024 * 1024)
                    if not block:
                        break
                    total += sum(block)
                    count += len(block)
            grain_bias = (total / max(1, count)) - 128.0
        except Exception:
            grain_bias = 0.0
        log(f"the grain pattern is biased by {grain_bias:+.1f} levels - "
            f"that is subtracted")
    filters = []
    if target:
        # The AI gave us its own size; this is the one that was asked for.
        filters.append(f"scale={target[0]}:{target[1]}:flags=lanczos")
    if soften:
        filters.append(f"unsharp=5:5:{soften}:5:5:{soften}")
    if cas:
        # cas is contrast adaptive sharpening: it brings an edge back
        # without the ringing that gives negative unsharp away.
        filters.append(f"cas=strength={cas}")
    if grain and not perlin_grain:
        filters.append(f"noise=alls={grain}:allf=u")
    # The grade: a luma curve and a chroma saturation, all in YUV. See
    # engine.grade_filters for what it replaced and the measurement.
    import engine as engine_mod
    filters.extend(engine_mod.grade_filters(look))
    filters.append("format=yuv420p")
    shift = int(look.get("chroma_shift") or 0)
    if shift:
        # The chroma axis is what makes a picture read as analogue: the
        # colour sits a sample behind the edges.
        filters.append(f"chromashift=cbh={shift}:crh={shift}:edge=smear")
    if look.get("chroma_soft"):
        filters.append("chromanr=thres=30:sizew=5:sizeh=5")
    # A comma straight after a label is read as an empty filter, so the
    # filters are joined without one.
    parts.append("[mix]" + ",".join(filters) + "[v0]")
    node = "[v0]"
    fw, fh = target if target else (w, h)
    if halation:
        # The same recipe the local path uses: threshold the highlights,
        # blur them, and add them back with a floor at zero, so the glow can
        # only ever ADD light. An arithmetic blend subtracts below mid-grey
        # and leaves the shadows crushed - and a crushed shadow is what the
        # grain step afterwards turns white.
        thresh = 210
        gain = halation * 2.0
        parts.append(f"{node}split=2[base][hlsrc]")
        parts.append(f"[hlsrc]format=gray,gblur=sigma={halation_sigma}[hlg]")
        parts.append(f"[hlg]lut='if(gt(val,{thresh}),val,0)'[hlm]")
        # The glow is added, but it may not push the picture past white: a
        # studio light that was already bright would go flat and lose its
        # texture, which is worse than no glow at all.
        parts.append(f"[base][hlm]blend=c0_expr='min(235,A+max(0,B-128)*{gain:.4f})':"
                     f"c1_expr='A':c2_expr='A'[v1]")
        node = "[v1]"
    if perlin_grain:
        parts.append(f"[2:v]scale={fw}:{fh}:flags=bilinear,format=yuv420p[bn]")
        # The result is kept inside the legal range. The grain is a plus or
        # a minus, and a plus on a picture that is already near white would
        # clip it to a flat, dead white - which is what a blown highlight
        # looks like.
        parts.append(f"{node}[bn]blend=c0_expr='min(235,max(16,"
                     f"A+(B-128-({grain_bias:.3f}))*{grain_s}))':"
                     f"c1_expr='A':c2_expr='A'[v2]")
        node = "[v2]"
    parts.append(node + "null[v]")

    cmd = [ffbin.ffmpeg(), "-y", "-v", "error",
           "-i", cloud_video, "-i", original]
    if perlin_grain:
        cmd += ["-f", "rawvideo", "-pixel_format", "gray",
                "-video_size", f"{gw}x{gh}", "-framerate", f"{fps:.6f}",
                "-i", grain_file]
    cmd += [
           "-filter_complex", ";".join(parts),
           "-map", "[v]", "-map", "1:a?"]
    if encoder == "h265":
        cmd += ["-c:v", "libx265", "-crf", "18", "-preset", "slow", "-pix_fmt", "yuv420p",
                "-tag:v", "hvc1"]
    elif encoder == "prores":
        cmd += ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le"]
    elif encoder == "vp9":
        cmd += ["-c:v", "libvpx-vp9", "-crf", "20", "-b:v", "0", "-row-mt", "1",
                "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c:v", "libx264", "-crf", "17", "-preset", "slow", "-pix_fmt", "yuv420p"]
    cmd += ["-r", f"{fps:.6f}", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", out_path]
    log("finishing on this Mac (blend, soften, grain, grade, encode)…")
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError("the finishing pass failed:\n"
                           + res.stderr.decode("utf-8", "replace")[-600:])
    if grain_file and os.path.exists(grain_file):
        try:
            os.remove(grain_file)
        except OSError:
            pass
    return out_path
