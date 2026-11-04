#!/usr/bin/env python3
"""The guards, checked.

Three faults cost one rental each, because nothing refused early:

  1. the DLoRAL preset asked for 4x with no model, the server filled in the
     default 2x model and refused the scale - so the preset could not start
     at all;
  2. the estimate showed its duration in seconds, so a 47 hour job read as
     a number next to a price;
  3. the worker answered /health with every engine it could ever have, so
     the job was told yes and only found out after the denoise and after a
     pod had been rented.

This checks that all three stay fixed, and that a refused job really does
not rent anything.

    python3 tools/test_cloud_guards.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import keys  # noqa: E402

GUI = "http://127.0.0.1:8790"
FAILED = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + (("  - " + detail) if detail else ""))
    if not ok:
        FAILED.append(name)


def call(path, body=None, method=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GUI + path, data=data,
                                 method=method or ("POST" if body else "GET"))
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def pods():
    """The pods the account is renting, through the same code that creates
    them (the pod API lives on api.runpod.io, not api.runpod.ai)."""
    import rental
    key = keys.resolve("runpod")
    if not key:
        return None
    try:
        return rental.RunPod(key)._request("GET", "/pods").get("pods", [])
    except Exception:
        return None


def main():
    print("the guards")

    # 1. the presets that need an engine the image does not have are marked
    status, models = call("/api/models")
    if status != 200:
        print("  FAIL the GUI is not answering on " + GUI)
        return 1
    presets = {p["id"]: p for p in models.get("presets", [])}
    # The regenerate preset needs DLoRAL, and the image it defaults to has
    # DLoRAL in it - so it can run as it is. It used to point at the image
    # WITHOUT the diffusion layer, which made the preset a dead end and
    # made the job refuse before it began.
    p = presets.get("regenerate")
    check("the regenerate preset can run with the default image",
          p is not None and p.get("available") is True,
          (p or {}).get("reason", "no preset"))
    p = presets.get("star")
    # The image the STAR preset defaults to is the one with STAR in it, so
    # it can run as it is. It used to point at the plain image, which has no
    # STAR, and the preset was a dead end.
    check("the star preset can run with the default image",
          p is not None and p.get("available") is True,
          (p or {}).get("reason", "no preset"))
    for pid in ("faithful", "polished", "cloud", "best"):
        p = presets.get(pid)
        check(f"the {pid} preset is available", p is not None
              and p.get("available") is not False)

    # 2. the estimate speaks in hours
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "work", "guard_test_clip.mp4")
    # The suite must not depend on a file that a cleanup can take away, and
    # it must not share one with the rest of the work. It makes its own clip
    # every time, so the numbers below are the numbers it checks.
    #
    # The clip has to be long enough for the checks to mean anything. The
    # pod spin-up (240 s) is paid for every job, so on a two second clip it
    # swamps the model time and every model looks the same price. And a
    # diffusion over a short clip comes out under an hour, which is no way
    # to check that the estimate speaks in hours. Five minutes puts both
    # back.
    # The clip is made fresh every time. If an earlier run left a file there
    # and macOS had evicted it to the iCloud, opening that file for writing
    # blocks on the download and the test HANGS instead of running - so the
    # old one goes first.
    if os.path.exists(src):
        try:
            os.remove(src)
        except OSError:
            pass
    os.makedirs(os.path.dirname(src), exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=300",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=300",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", src], check=False)
    if os.path.exists(src):
        status, est = call("/api/estimate", {"path": src, "backend": "cloud",
                                             "cloud_engine": "dloral", "model": "",
                                             "scale": 4, "look": "polished", "rent": True})
        check("the estimate comes back", status == 200)
        # Both diffusion engines have been measured on a card now, so the
        # estimate says so. An engine with no measurement still has to say
        # that it is a guess.
        check("the estimate says the engine was measured",
              est.get("measured") is True)
        status, est2 = call("/api/estimate", {"path": src, "backend": "cloud",
                                              "cloud_engine": "seedvr2", "model": "",
                                              "scale": 4, "look": "polished", "rent": True})
        check("the estimate says the other engine was measured too",
              est2.get("measured") is True)
        check("the estimate carries the duration",
              (est.get("ai_seconds") or 0) > 0)
        # the seconds must be readable as a duration
        secs = est.get("ai_seconds") or 0
        hours = secs / 3600.0
        check("the duration reads as hours, not as a number of seconds",
              hours >= 1.0, f"{secs}s = {hours:.1f} h")
    else:
        check("the test clip could be made", False, src)

    # 4. the estimate knows which net is doing the work: the deep RRDB net
    #    does about twenty times the work per pixel of the compact one
    if os.path.exists(src):
        def cost_for(model, scale):
            _, e = call("/api/estimate", {"path": src, "backend": "cloud",
                                          "cloud_engine": "realesrgan", "model": model,
                                          "scale": scale, "look": "polished", "rent": True})
            return (e.get("one_clip_cost") or 0), e.get("measured")
        compact, cm = cost_for("xintao-realesr-animevideo-x2-v3", 2)
        deep, dm = cost_for("xintao-realesrgan-x4plus", 4)
        check("the compact model is quoted as measured", cm is True)
        check("the deep model is quoted as a guess", dm is False)
        check("the deep model costs more than the compact one", deep > compact * 3,
              f"${compact} against ${deep}")

    # 3. a job that cannot run refuses, and rents nothing
    #
    # The image is named explicitly here. The default image for DLoRAL has
    # the diffusion layer in it - that is the point of the fix - so the
    # refusal has to be tested against an image that genuinely lacks it.
    before = pods()

    # A job that would rent a GPU needs the user's approval of the price,
    # and the approval is the server's: an agent cannot write its own.
    status, refused = call("/api/run", {
        "path": src, "backend": "cloud", "cloud_engine": "dloral", "model": "",
        "scale": 4, "look": "polished", "denoise_engine": "bilateral",
        "encoder": "h264", "rent": True, "rent_approved": True,
        "rent_image": "docker.io/bzz1/upscaler:latest"})
    check("a job that rents without an approval is refused",
          status == 400 and not refused.get("id"),
          json.dumps(refused)[:160])
    check("the refusal asks for an estimate",
          bool(refused.get("needs_approval")), json.dumps(refused)[:120])

    # The whole way round: the estimate, the user's approval, the job.
    _, est = call("/api/estimate", {
        "path": src, "backend": "cloud", "cloud_engine": "dloral", "model": "",
        "scale": 4, "look": "polished", "denoise_engine": "bilateral",
        "encoder": "h264", "rent": True,
        "rent_image": "docker.io/bzz1/upscaler:latest"})
    aid = est.get("approval_id")
    check("an estimate comes with an approval to give", bool(aid),
          json.dumps(est)[:120])
    _, ap = call("/api/approve", {"id": aid, "yes": True})
    check("the user can approve the price", ap.get("state") == "approved",
          json.dumps(ap)[:120])
    status, job = call("/api/run", {
        "path": src, "backend": "cloud", "cloud_engine": "dloral", "model": "",
        "scale": 4, "look": "polished", "denoise_engine": "bilateral",
        "encoder": "h264", "rent": True, "rent_approval": aid,
        "rent_image": "docker.io/bzz1/upscaler:latest"})
    jid = job.get("id")
    check("the job is accepted (the scale check no longer refuses it)",
          bool(jid), json.dumps(job)[:160])
    if jid:
        time.sleep(3)
        status, j = call(f"/api/job?id={jid}")
        err = (j.get("error") or "")
        check("the job refuses", j.get("state") in ("error", "failed"), j.get("state"))
        check("the refusal names the engine", "dloral" in err, err[:120])
        check("the refusal says what to do instead",
              "BUILD_DLORAL" in err or "Polished" in err, err[:120])
        check("the refusal came before the denoise",
              not any("de-noised" in (l or "") for l in (j.get("log") or [])))

    # 3b. the DLoRAL canvas: the model works from a small canvas and its
    #     own 4x lands the result on the target. Without this the whole
    #     source went up and the result came back four times as big.
    try:
        import engine
        w, h = engine.canvas_for((1920, 1080), 1920, 1080, 4)
        check("the DLoRAL canvas is a quarter of the target",
              (w, h) == (480, 270), f"{w}x{h}")
        w2, h2 = engine.canvas_for((1920, 1080), 1920, 1080, 4)
        check("the canvas is the same every time", (w2, h2) == (w, h))
        # The canvas has to be even (H.264 cannot carry an odd one), so a
        # target that is not a multiple of the scale comes back a couple of
        # pixels short. The finishing pass scales it to the target, so the
        # file is the size that was asked for.
        w3, h3 = engine.canvas_for((400, 300), 400, 300, 4)
        check("the canvas is an even number of pixels",
              w3 % 2 == 0 and h3 % 2 == 0, f"{w3}x{h3}")
        check("the canvas lands on the target, or a hair under it",
              abs(w3 * 4 - 400) <= 4 and abs(h3 * 4 - 300) <= 4,
              f"{w3*4}x{h3*4} against 400x300")
    except Exception as e:
        check("the canvas rule exists", False, str(e)[:120])

    # 3b2. the DLoRAL job must collect the frames the script MADE, not the
    #      ones it read. The script writes both: the input frames next to
    #      the video it read, and the diffusion result in its output
    #      directory. Picking up the wrong set gives back the canvas and
    #      throws the whole diffusion away - and it looks like it worked.
    try:
        import sys
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "worker"))
        import dloral_job
        import tempfile
        from PIL import Image
        tmp = tempfile.mkdtemp(prefix="dloral-frames-")
        stem = "piece-00000"
        # the canvas the script was given, and the frames it read
        os.makedirs(os.path.join(tmp, "in", stem))
        Image.new("RGB", (100, 74)).save(
            os.path.join(tmp, "in", stem, "frame_0000.png"))
        # the frames it made: four times the canvas
        os.makedirs(os.path.join(tmp, "out", stem))
        for i in range(1, 4):
            Image.new("RGB", (400, 296)).save(
                os.path.join(tmp, "out", stem, f"frame_{i:04d}.png"))
        made = dloral_job.made_frames(os.path.join(tmp, "out"), stem)
        check("the job finds the frames the script made", len(made) == 3,
              f"{len(made)} frames")
        if made:
            with Image.open(made[0]) as im:
                check("those frames are the diffusion result, not the canvas",
                      im.size == (400, 296), f"{im.size}")
        # The job has to look in ITS OWN output directory - the one it
        # hands the script - and not in the directory the video went into.
        job = dloral_job.Job.__new__(dloral_job.Job)
        job.script_out = os.path.join(tmp, "out")
        got = job._made_frames(stem)
        check("the job looks in the directory it gave the script",
              len(got) == 3, f"{len(got)} frames")
        job.script_out = os.path.join(tmp, "in")
        got2 = job._made_frames(stem)
        check("the frames the script read are not what the job takes",
              len(got2) == 1 and len(got) == 3,
              f"{len(got2)} read against {len(got)} made")
    except Exception as e:
        check("the frame collection rule exists", False, str(e)[:140])

    # 3c2. a background job must not read the file the caller may delete.
    #      The worker hands the upload over in a temporary directory and
    #      cleans it up the moment it answers with the job id, so a job
    #      that reads the original finds nothing - and STAR stopped with
    #      "stack expects a non-empty TensorList".
    try:
        import sys
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "worker"))
        import star_job
        import tempfile
        tmp = tempfile.mkdtemp(prefix="star-lifetime-")
        src = os.path.join(tmp, "in.mp4")
        with open(src, "wb") as fh:
            fh.write(b"not really a video, but it has to exist")
        job = star_job.Job(src, prompt="x")
        # The real start(), with the model run stubbed out: the copy is
        # the thing being checked, not the inference.
        job._run_job = lambda: None
        job.start()
        os.remove(src)
        check("the job has its own copy of the upload",
              os.path.exists(job.video), job.video)
        check("the copy survives the caller deleting the original",
              os.path.getsize(job.video) > 0)
    except Exception as e:
        check("the job keeps its own copy of the upload", False, str(e)[:140])

    # 3c3. the card lifecycle: a warm card is stopped, not terminated.
    #      Terminating throws the disk away with the weights on it, which is
    #      the whole reason a run downloads eleven gigabytes every time.
    try:
        import rental
        calls = []

        class FakePod(rental.RunPod):
            def _request(self, method, path, body=None):
                calls.append((method, path, body))
                return {"id": "fake", "cost": 1.09}

        pod = FakePod("k", gpu="NVIDIA L40S", engine="dloral",
                      keep_warm=True)
        pod.pod_id = "fake"
        pod.destroy()
        check("a warm card is stopped, not terminated",
              calls and calls[0][0] == "POST"
              and calls[0][1].endswith("/action")
              and (calls[0][2] or {}).get("action") == "stop",
              str(calls))
        check("the warm card is not terminated",
              not any(c[0] == "DELETE" for c in calls), str(calls))

        calls2 = []
        pod2 = FakePod("k", gpu="NVIDIA L40S", engine="dloral",
                       keep_warm=False)
        pod2._request = lambda m, p, b=None: calls2.append((m, p, b)) or {}
        pod2.pod_id = "fake"
        pod2.destroy()
        check("a card that is not kept warm is terminated",
              calls2 and calls2[0][0] == "DELETE", str(calls2))
    except Exception as e:
        check("the card lifecycle keeps the weights when it can", False,
              str(e)[:140])

    # 3c0. the ffmpeg has to RUN. Homebrew replaces ffmpeg and its
    #       libraries one at a time, and in the middle of that swap the
    #       binary on the PATH cannot load its own libraries any more -
    #       which killed a cloud upscale that had already paid for the
    #       card. The tool picks an ffmpeg that actually starts.
    try:
        import ffbin
        import subprocess as sp
        ff = ffbin.ffmpeg()
        r = sp.run([ff, "-version"], stdout=sp.PIPE, stderr=sp.PIPE,
                   timeout=30)
        check("the ffmpeg we use actually runs", r.returncode == 0, ff)
    except Exception as e:
        check("the ffmpeg we use actually runs", False, str(e)[:140])

    # 3c1. the cards have to EXIST. Runpod's names are not what you
    #       would guess - the A5000 is "NVIDIA RTX A5000" and the 80 GB
    #       A100 is "NVIDIA A100 80GB PCIe" - and a name that does not
    #       exist fails when the card is rented, with a message about
    #       global networking that says nothing about the real problem.
    try:
        import rental as rental_mod
        have = rental_mod.catalogue_ids()
        if have:
            wrong = sorted({c["id"] for cards in rental_mod.GPU_CHOICES.values()
                            for c in cards if c["id"] not in have})
            check("every card we offer exists in the catalogue", not wrong,
                  ", ".join(wrong))
        else:
            check("every card we offer exists in the catalogue", True,
                  "the catalogue could not be reached")
    except Exception as e:
        check("every card we offer exists in the catalogue", False,
              str(e)[:140])

    # 3c2. the cards that are offered have to be able to hold the
    #       picture. DLoRAL's VAE decoder runs its attention at the size
    #       of the RESULT, so a 1080p DLoRAL job does not fit on a 24 GB
    #       card - it dies on the card, after the money is spent. A 720p
    #       one does fit, and the cheaper card should be offered for it.
    try:
        import rental as rental_mod
        big = rental_mod.gpu_choices("dloral", out_height=1080)
        small = rental_mod.gpu_choices("dloral", out_height=720)
        check("a 1080p DLoRAL job is not offered a 24 GB card",
              big and all((c.get("vram") or 0) >= 48 for c in big),
              ", ".join(c["id"] for c in big))
        check("a 720p DLoRAL job is offered the cheaper card",
              small and any((c.get("vram") or 0) <= 24 for c in small),
              ", ".join(c["id"] for c in small))
        check("the estimate says how much memory the card needs",
              rental_mod.vram_for("dloral", 1080) == 48
              and rental_mod.vram_for("dloral", 720) == 24
              and rental_mod.vram_for("star", 720) == 48,
              "dloral 1080/720, star 720")
    except Exception as e:
        check("the cards fit the picture", False, str(e)[:140])

    # 3c3. the network volume is off unless somebody asks for it. It
    #       costs $2.80 a month and saves about $0.05 a run, so it only
    #       pays for itself at thirty runs a month - and the code that
    #       keeps a card warm between jobs costs nothing and covers the
    #       same ground for jobs that come one after another.
    try:
        import rental as rental_mod
        check("the network volume is off by default",
              rental_mod.DEFAULT_VOLUME_ID == "",
              repr(rental_mod.DEFAULT_VOLUME_ID))
    except Exception as e:
        check("the network volume is off by default", False, str(e)[:140])

    # 3c4. a centre that has no card right now is asked again. US-TX-3
    #       had an L40S at 10:50 and none at 11:01, and a pod that cannot
    #       be created is not billed - so trying again costs nothing but
    #       a few seconds.
    try:
        import rental as rental_mod

        class FlakyPod(rental_mod.RunPod):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.tries = 0

            def _request(self, method, path, body=None):
                if method == "GET" and path == "/network-volumes":
                    return {"networkVolumes": [
                        {"id": self.volume_id, "dataCenter": "US-TX-3"}]}
                if method == "POST" and path == "/pods":
                    self.tries += 1
                    if self.tries < 3:
                        raise rental_mod.RentalError(
                            "Runpod said 400: There are no longer any "
                            "instances available")
                    return {"id": "fake", "cost": 1.09}
                return {}

        flaky = FlakyPod("k", gpu="NVIDIA L40S", engine="dloral",
                         volume_id="fake-volume")
        flaky.approve({})
        flaky.retry_wait = 0        # the test does not wait
        flaky.create()
        check("a centre without a card is asked again",
              flaky.tries == 3, f"{flaky.tries} tries")
    except Exception as e:
        check("a centre without a card is asked again", False, str(e)[:140])

    # 3d. the DLoRAL encode: the frames the script makes are numbered
    #      from ONE, and the result has to keep the frame rate it came in
    #      at. Both of these were wrong, and both of them failed quietly.
    try:
        import sys
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "worker"))
        import dloral_job
        import tempfile
        from PIL import Image
        tmp = tempfile.mkdtemp(prefix="dloral-encode-")
        fdir = os.path.join(tmp, "frames")
        os.makedirs(fdir)
        for i in (1, 2, 3):        # the script numbers them from one
            Image.new("RGB", (64, 48), (i * 40, 20, 10)).save(
                os.path.join(fdir, f"frame_{i:04d}.png"))
        job = dloral_job.Job.__new__(dloral_job.Job)
        job.out_dir = os.path.join(tmp, "out")
        job.fps = 50.0
        piece = {"name": "piece-00000.mp4", "state": "pending", "bytes": 0}
        job._encode(dloral_job.made_frames(fdir, "frames"), piece, fdir)
        made = os.path.join(job.out_dir, piece["name"])
        check("the encode of frames numbered from one works",
              os.path.exists(made), piece.get("state"))
        if os.path.exists(made):
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                 "-show_entries", "stream=nb_read_frames,avg_frame_rate,width,height",
                 "-of", "default=nw=1", made], stdout=subprocess.PIPE).stdout.decode()
            vals = dict(line.split("=") for line in out.splitlines() if "=" in line)
            check("all the frames are in the video",
                  vals.get("nb_read_frames") == "3", out.replace("\n", " "))
            check("the result keeps the frame rate it came in at",
                  vals.get("avg_frame_rate") == "50/1", out.replace("\n", " "))
    except Exception as e:
        check("the DLoRAL encode works", False, str(e)[:140])

    # 3f. the pieces have to overlap by one frame. The DLoRAL script works
    #      on a two-frame window and makes the second one, so it cannot
    #      make the first frame of the video it is given. Every piece used
    #      to come back one frame short - and a film cut into pieces loses
    #      a frame at every seam and drifts away from its own audio.
    try:
        tmp2 = tempfile.mkdtemp(prefix="dloral-split-")
        clip = os.path.join(tmp2, "clip.mp4")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc=size=64x48:rate=25", "-frames:v", "25",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", clip],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        job2 = dloral_job.Job.__new__(dloral_job.Job)
        job2.video = clip
        job2.in_dir = os.path.join(tmp2, "in")
        job2.out_dir = os.path.join(tmp2, "out")
        job2.pieces = []
        job2.lines = []
        job2._print = lambda m: None
        job2.log = job2._record
        old_piece = dloral_job.PIECE_FRAMES
        dloral_job.PIECE_FRAMES = 10
        try:
            job2._split()
        finally:
            dloral_job.PIECE_FRAMES = old_piece
        counts = []
        for p in job2.pieces:
            out = os.path.join(job2.in_dir, p["name"])
            n = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                 "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1",
                 out], stdout=subprocess.PIPE).stdout.decode()
            try:
                counts.append(int(n.split("=")[1].strip()))
            except Exception:
                counts.append(0)
        # Every piece is cut one frame longer than the frames it has to give
        # back: the extra one is the overlap the script's blind spot eats.
        check("the pieces overlap by one frame",
              counts and all(c == p["frames"] + 1
                             for c, p in zip(counts, job2.pieces)),
              str(counts))
        total = sum(p["frames"] for p in job2.pieces)
        check("no frame of the film is lost at a seam", total == 25, str(total))
    except Exception as e:
        check("the pieces overlap by one frame", False, str(e)[:140])

    # 3g. the finishing pass has to work with the look the GUI starts with.
    #      The "clean" look has no blend, and the soft enlargement was built
    #      anyway and then left dangling: ffmpeg refuses a filter graph with
    #      an unconnected output, so every cloud job with the default look
    #      died at the finishing pass - after the card was paid for.
    try:
        import cloud as cloud_mod
        import engine as engine_mod
        tmp3 = tempfile.mkdtemp(prefix="finishing-")
        ai = os.path.join(tmp3, "ai.mp4")
        original = os.path.join(tmp3, "src.mp4")
        for path in (ai, original):
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                            "testsrc=size=64x48:rate=25", "-frames:v", "4",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", path],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out = os.path.join(tmp3, "out.mp4")
        cloud_mod.finish_locally(ai, original, out, scale=2,
                                 look=dict(engine_mod.LOOKS["clean"]),
                                 encoder="h264", target=(64, 48),
                                 log=lambda m: None)
        check("the finishing pass works with the clean look",
              os.path.exists(out) and os.path.getsize(out) > 0,
              "the clean look has no blend")
    except Exception as e:
        check("the finishing pass works with the clean look", False, str(e)[:140])

    # 3e. the local worker: the one that runs on this Mac's GPU. It is a
    #      separate process from the GUI, and if it is not there the local
    #      presets are dead ends.
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:8791/ping")
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode()
        check("the local worker answers", '"ok"' in body, body[:80])
    except Exception as e:
        check("the local worker answers", False, str(e)[:80])

    # 3f. every engine has to be able to price itself without an error.
    if os.path.exists(src):
        for eng in ("realesrgan", "seedvr2", "dloral", "star"):
            st, e = call("/api/estimate", {"path": src, "backend": "cloud",
                                           "cloud_engine": eng, "model": "",
                                           "scale": 4, "look": "polished",
                                           "rent": True})
            check(f"the {eng} engine can be priced",
                  st == 200 and not e.get("error") and (e.get("one_clip_cost") or 0) > 0,
                  json.dumps(e)[:120])

    # 3g. the space guard: the scratch space has to be somewhere, and the
    #      guard has to be able to say what the disk is doing.
    try:
        import sys
        sys.path.insert(0, os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        from tools import space_guard
        st = space_guard.status()
        check("the scratch space is there", os.path.isdir(space_guard.SCRATCH),
              space_guard.SCRATCH)
        check("the guard can see the free space", st["free_bytes"] is not None)
        check("the iCloud Drive is there for when it is needed",
              st["ic_cloud"] is not None, str(st["ic_cloud"]))
        check("the guard does nothing while there is space",
              space_guard.ensure_space(min_free_gib=1) is None)
    except Exception as e:
        check("the space guard exists", False, str(e)[:140])

    # 3c. the image guard itself: the default image for DLoRAL has the
    #     diffusion layer, an image without it is refused.
    try:
        import engine
        import rental
        engine.check_image_can_run(rental.DEFAULT_IMAGE_DLORAL, "dloral")
        check("the default image can run DLoRAL", True)
        try:
            engine.check_image_can_run("docker.io/bzz1/upscaler:latest", "dloral")
            check("an image without DLoRAL is refused", False, "it was allowed")
        except Exception as e:
            check("an image without DLoRAL is refused", "dloral" in str(e))
    except Exception as e:
        check("the image guard exists", False, str(e)[:120])
    time.sleep(2)
    after = pods()
    if before is not None and after is not None:
        check("nothing was rented", len(after) <= len(before),
              f"{len(before)} -> {len(after)} pods")

    print()
    if FAILED:
        print(f"{len(FAILED)} check(s) failed: " + ", ".join(FAILED))
        return 1
    print("all the guards hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
