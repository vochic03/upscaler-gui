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

import json
import os
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
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def upscale(endpoint, video_path, out_path, scale=2, model=None, denoise=None,
            api_key=None, timeout=7200, log=print, engine="realesrgan",
            resolution=None, prompt=None):
    """Send the video to the worker, save what comes back."""
    url = endpoint.rstrip("/") + "/upscale"
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
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", src]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-b:v", "10M", "-maxrate", "10M",
            "-bufsize", "20M", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", dst]
    if perlin_grain:
        cmd += ["-shortest"]
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
    grain_s = grain * 0.05
    halation_sigma = max(4, min(24, (target[1] if target else h) // 90))
    contrast = float(look.get("contrast", 0.0))
    saturation = float(look.get("saturation", 0.0))

    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
         cloud_video], stdout=subprocess.PIPE).stdout.decode())
    v = next(s for s in info["streams"] if s.get("codec_type") == "video")
    w, h = int(v["width"]), int(v["height"])
    try:
        num, den = (v.get("avg_frame_rate") or "25/1").split("/")
        fps = float(num) / float(den) if float(den) else 25.0
    except Exception:
        fps = 25.0

    parts = [f"[0:v]setpts=PTS-STARTPTS,fps={fps:.6f}[ai]"]
    parts.append(f"[1:v]scale={w}:{h}:flags=lanczos,setsar=1,fps={fps:.6f},"
                 f"setpts=PTS-STARTPTS[soft]")
    if blend > 0:
        parts.append(f"[ai][soft]blend=all_expr='A*(1-{blend})+B*{blend}'[mix]")
    else:
        parts.append("[ai]null[mix]")
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
    if contrast or saturation:
        filters.append(f"eq=contrast={1 + contrast}:saturation={1 + saturation}")
    lift = float(look.get("black_lift") or 0)
    if lift:
        # Lift the black point a touch: the shadow detail of an analogue
        # transfer never reaches zero.
        filters.append(f"colorlevels=rimin={lift}:gimin={lift}:bimin={lift}")
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
        parts.append(f"{node}split=2[base][hlsrc]")
        parts.append(f"[hlsrc]format=gray,gblur=sigma={halation_sigma},"
                     f"format=yuv420p[hlb]")
        parts.append(f"[base][hlb]blend=c0_expr='A+(B-128)*{halation}':"
                     f"c1_expr='A':c2_expr='A'[v1]")
        node = "[v1]"
    if perlin_grain:
        parts.append(f"[2:v]scale={fw}:{fh}:flags=bilinear,format=yuv420p[bn]")
        parts.append(f"{node}[bn]blend=c0_expr='A+(B-128)*{grain_s}':"
                     f"c1_expr='A':c2_expr='A'[v2]")
        node = "[v2]"
    parts.append(node + "null[v]")

    cmd = ["ffmpeg", "-y", "-v", "error",
           "-i", cloud_video, "-i", original]
    if perlin_grain:
        gw = max(2, (target[0] if target else w) // 2)
        gh = max(2, (target[1] if target else h) // 2)
        cmd += ["-f", "lavfi", "-i",
                f"perlin=size={gw}x{gh}:rate={fps:.6f}:"
                f"tscale={look.get('grain_tscale') or 0.08}:octaves=2:"
                f"persistence=0.5:random_mode=seed:random_seed=1"]
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
    return out_path
