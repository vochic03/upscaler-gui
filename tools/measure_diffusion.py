#!/usr/bin/env python3
"""Price the diffusion routes on a rented card.

    python3 tools/measure_diffusion.py --image bzz1/upscaler:seedvr2 --yes

The question is which route is worth taking for the film:

  * **SeedVR2 at 480p** - restore the picture at 480p and build the 1080p
    from that. The model works at the output resolution, so 480p is about a
    fifth of the price of 1080p.
  * **SeedVR2 at 1080p** - the native route: the diffusion runs at 1080p and
    the result is used as it is.
  * **DLoRAL at 4x** - its trained factor, so the diffusion runs at 1080p
    too, but the input is a 270p canvas.

The two SeedVR2 runs take the SAME input - the first 25 frames of the film
at 1080p - so the only difference between them is the output resolution.
That is the comparison that decides the route.

Two timings per resolution, not one. Every invocation of the SeedVR2 CLI
loads the model first, and that load is a fixed cost of the run - it is
nothing to do with how fast the frames go. One timing of 25 frames would
fold it in and make the route look a third slower than it is. So each
resolution is run twice, with 5 frames and with 25, and the per-frame time
comes from the difference. The load, and the download of the weights on the
first run, cancel out.

Only the engines the image actually has are measured. The worker says so
on /health, so nothing is measured that the image cannot run.
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cloud  # noqa: E402
import keys  # noqa: E402
import rental  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "work", "difftest")
SRC = os.path.join(ROOT, "work", "uploads",
                   "capture-2026-10-29T08-25-58-212Z-reformatted-00.01.31.668-00.02.43.840 - 01.mp4")
FRAMES = 25
SHORT_FRAMES = 5
FILM_FRAMES = 3609
# DLoRAL is a 4x model, so its diffusion runs at the full 1080p and it is
# an order of magnitude slower than SeedVR2. The same 25 frames would
# take half an hour of card time to measure, which is money for a number
# that comes out of a ten frame clip just as well.
DLORAL_FRAMES = 13

# The card, in the order they are tried. The first one with stock wins.
CARDS = ("NVIDIA GeForce RTX 4090", "NVIDIA L40S", "NVIDIA RTX A5000",
         "NVIDIA A40", "NVIDIA GeForce RTX 3090 Ti", "NVIDIA RTX A6000",
         "NVIDIA L4", "NVIDIA RTX 4000 Ada Generation")


def run(cmd):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[-300:])


def prep():
    """The clips to measure on: the film at 1080p, the same at 480p, a 270p
    canvas for DLoRAL, and a short one to time the model load with."""
    os.makedirs(WORK, exist_ok=True)
    full = os.path.join(WORK, "full.mp4")
    short = os.path.join(WORK, "short.mp4")
    half = os.path.join(WORK, "half.mp4")
    half_short = os.path.join(WORK, "half-short.mp4")
    quarter = os.path.join(WORK, "quarter.mp4")
    quarter_short = os.path.join(WORK, "quarter-short.mp4")
    quarter_full = os.path.join(WORK, "quarter-full.mp4")
    tiny = os.path.join(WORK, "tiny.mp4")
    tiny_short = os.path.join(WORK, "tiny-short.mp4")
    cloud.split_video(SRC, WORK, 0, FRAMES, 50.0)
    part = os.path.join(WORK, "part-0000000.mp4")
    os.replace(part, full)
    run(["ffmpeg", "-y", "-v", "error", "-i", full,
         "-t", "0.1", "-c:v", "libx264", "-preset", "veryfast", "-crf", "14",
         "-pix_fmt", "yuv420p", "-an", short])
    # the same film at 480p, so the cost of a 480p INPUT can be told apart
    # from the cost of a 480p OUTPUT
    run(["ffmpeg", "-y", "-v", "error", "-i", full,
         "-vf", "scale=854:480:flags=lanczos", "-c:v", "libx264",
         "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p",
         "-an", half])
    run(["ffmpeg", "-y", "-v", "error", "-i", half,
         "-t", "0.1", "-c:v", "libx264", "-preset", "veryfast", "-crf", "14",
         "-pix_fmt", "yuv420p", "-an", half_short])
    run(["ffmpeg", "-y", "-v", "error", "-i", full,
         "-vf", "scale=480:270:flags=lanczos", "-c:v", "libx264",
         "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p",
         "-an", quarter])
    run(["ffmpeg", "-y", "-v", "error", "-i", quarter,
         "-t", "0.1", "-c:v", "libx264", "-preset", "veryfast", "-crf", "14",
         "-pix_fmt", "yuv420p", "-an", quarter_short])
    run(["ffmpeg", "-y", "-v", "error", "-i", full,
         "-vf", "trim=start_frame=0:end_frame=%d,scale=480:270:flags=lanczos,"
         "setpts=PTS-STARTPTS" % DLORAL_FRAMES,
         "-r", "50", "-c:v", "libx264", "-preset", "veryfast", "-crf", "14",
         "-pix_fmt", "yuv420p", "-an", quarter_full])
    # a 120p canvas: DLoRAL is a 4x model, so this comes out at 480p. It
    # is the route that fits in 24 GB of VRAM - at 1080p the VAE decoder's
    # attention wants 16 GB on its own.
    run(["ffmpeg", "-y", "-v", "error", "-i", full,
         "-vf", "trim=start_frame=0:end_frame=%d,scale=214:120:flags=lanczos,"
         "setpts=PTS-STARTPTS" % DLORAL_FRAMES,
         "-r", "50", "-c:v", "libx264", "-preset", "veryfast", "-crf", "14",
         "-pix_fmt", "yuv420p", "-an", tiny])
    run(["ffmpeg", "-y", "-v", "error", "-i", tiny,
         "-t", "0.1", "-c:v", "libx264", "-preset", "veryfast", "-crf", "14",
         "-pix_fmt", "yuv420p", "-an", tiny_short])
    return full, short, half, half_short, quarter, quarter_short, quarter_full, tiny, tiny_short


def timed(endpoint, clip, out, engine, log, **kw):
    """Run one diffusion pass and return how long it took.

    The poll interval matters here. The Mac asks the worker how far the
    job has got every twenty seconds, so a short run comes back in one
    poll cycle whatever it really took - and a measurement that rounds
    every run up to the next twenty seconds cannot tell a 5 second run
    from a 19 second one. So it polls fast here.
    """
    t0 = time.time()
    cloud.diffusion_run(endpoint, clip, out, engine=engine, log=log,
                        poll=1.5, **kw)
    return time.time() - t0


def measure(endpoint, clip, short_clip, engine, resolution, log,
            frames=None):
    """The per-frame time at this resolution, and the fixed cost around it.

    Three runs at the same resolution. Every invocation of the CLI loads
    the model first, and that load is a fixed cost of the run - it is
    nothing to do with how fast the frames go, so one timing of 25 frames
    would fold it in and make the route look a third slower than it is.

    The FIRST run at a resolution also pays a one-off cost of its own:
    the kernels are compiled for that shape and cuDNN picks its
    algorithms. So the short run is done twice - the first one is thrown
    away - and the per-frame time comes from the difference between the
    second short run and the full one.
    """
    timed(endpoint, short_clip, os.path.join(WORK, f"{engine}-{resolution}-warm.mp4"),
          engine, log, resolution=resolution, batch_size=5)
    t_short = timed(endpoint, short_clip, os.path.join(WORK, f"{engine}-{resolution}-short.mp4"),
                    engine, log, resolution=resolution, batch_size=5)
    t_full = timed(endpoint, clip, os.path.join(WORK, f"{engine}-{resolution}.mp4"),
                   engine, log, resolution=resolution, batch_size=5)
    frames = (frames or FRAMES) - SHORT_FRAMES
    per_frame = (t_full - t_short) / frames
    if per_frame <= 0:
        raise RuntimeError(f"the {frames} extra frames took no time "
                           f"({t_short:.0f}s then {t_full:.0f}s)")
    fixed = t_short - SHORT_FRAMES * per_frame
    return per_frame, t_short, t_full, fixed


def main():
    image = "bzz1/upscaler:seedvr2"
    yes = "--yes" in sys.argv
    for i, a in enumerate(sys.argv):
        if a == "--image" and i + 1 < len(sys.argv):
            image = sys.argv[i + 1]
    if not os.path.exists(SRC):
        print("the source clip is not there: " + SRC, file=sys.stderr)
        return 1

    (full, short, half, half_short, quarter, quarter_short, quarter_full,
     tiny, tiny_short) = prep()
    key = keys.resolve("runpod")
    if not key:
        print("there is no Runpod key to rent with", file=sys.stderr)
        return 1

    r = rental.RunPod(key, gpu=CARDS[0], image=image, name="diffusion-test",
                      disk=80, port=8000, log=lambda m: print("  ", m, flush=True))
    print(f"the image: {image}")
    if not yes:
        print("\nThis would rent a real GPU. Run it with --yes to go ahead.")
        return 1

    r.approve({"one_clip_cost": 1})
    t0 = time.time()
    try:
        endpoint = None
        for gpu in CARDS:
            r.gpu = gpu
            try:
                r.create()
                endpoint = r.wait_ready(timeout=900)
                print(f"the card: {gpu} at ${r.price_per_hour()}/h")
                break
            except Exception as e:
                print(f"  no {gpu}: {str(e)[:110]}")
                r.pod_id = None
        if not endpoint:
            raise RuntimeError("no card was available")
        print(f"the worker answered after {time.time()-t0:.0f}s")

        try:
            have = (cloud.health(endpoint) or {}).get("engines") or []
        except Exception as e:
            have = []
            print("  (could not ask the worker what it has: " + str(e)[:80] + ")")
        print(f"the worker has: {', '.join(have) if have else '(it would not say)'}")

        # --- the routes -------------------------------------------------
        # (name, engine, clip, short clip, resolution)
        only = None
        for i, a in enumerate(sys.argv):
            if a == "--engine" and i + 1 < len(sys.argv):
                only = sys.argv[i + 1].lower()
        routes = []
        if "seedvr2" in have and only in (None, "seedvr2", "all"):
            routes.append(("SeedVR2 at 1080p (the native route)",
                           "seedvr2", full, short, 1080))
            routes.append(("SeedVR2 at 480p out, from a 1080p input",
                           "seedvr2", full, short, 480))
            routes.append(("SeedVR2 at 480p out, from a 480p input",
                           "seedvr2", half, half_short, 480))
        if "dloral" in have and only in (None, "dloral", "all"):
            routes.append(("DLoRAL at 4x to 480p, from a 120p canvas",
                           "dloral", tiny, tiny_short, 480))
            # The 1080p route is not measured here: the VAE decoder's
            # attention at 1080p wants 16 GB on its own and a 24 GB card
            # runs out of memory. It needs an L40S or an A100. The 480p
            # route below is measured, and the 1080p one follows from the
            # pixel ratio.

        if not routes:
            raise RuntimeError("the image has no diffusion engine in it")

        # --- the warm-up ------------------------------------------------
        # The first job on a cold card fetches the weights (3.4 GB for
        # SeedVR2, 11 GB for DLoRAL) and loads the model. That has nothing
        # to do with the per frame rate, and it would land on the first
        # timed run and make the two-point measurement come out as "the
        # extra frames took no time". So it is paid for first, and thrown
        # away.
        #
        # It runs the ROUTE'S OWN clip, not the big one. DLoRAL is a 4x
        # model: given the 1080p film it would produce a 7680x4320
        # picture, and the VAE decoder's attention at that size wants 16 GB
        # on its own. The warm-up has to be the same size as the thing it
        # is warming up.
        label0, engine0, clip0, short0, res0 = routes[0]
        print(f"warming up the {engine0} (the weights and the model load)…")
        tw = time.time()
        try:
            timed(endpoint, short0, os.path.join(WORK, "warm-out.mp4"),
                  engine0, lambda m: print("   ", m, flush=True),
                  resolution=res0, batch_size=5)
            print(f"  the warm-up took {time.time()-tw:.0f}s\n")
        except Exception as e:
            print("  the warm-up failed: " + str(e)[:400])
            raise

        # --- the measurements -------------------------------------------
        print()
        results = []
        for label, engine, clip, short_clip, res in routes:
            print(label)
            try:
                n = DLORAL_FRAMES if engine == "dloral" else FRAMES
                per_frame, t_short, t_full, fixed = measure(
                    endpoint, clip, short_clip, engine, res,
                    lambda m: print("   ", m, flush=True), frames=n)
            except Exception as e:
                print(f"  FAILED: {str(e)[:500]}\n")
                continue
            fps = 1.0 / per_frame
            hours = FILM_FRAMES / fps / 3600.0
            cost = hours * r.price_per_hour()
            results.append((label, fps, hours, cost))
            print(f"  {SHORT_FRAMES} frames in {t_short:.0f}s, {n} frames "
                  f"in {t_full:.0f}s")
            print(f"  {per_frame:.2f}s a frame = {fps:.2f} frames/s "
                  f"(the fixed cost around it: {fixed:.0f}s)")
            print(f"  the film would take {hours:.1f} h and cost "
                  f"${cost:.2f} on a {r.gpu}\n")

        print("=" * 72)
        for label, fps, hours, cost in results:
            print(f"{label:48s} {fps:5.2f} f/s {hours:5.1f} h  ${cost:6.2f}")
        print(f"the card was {r.gpu} at ${r.price_per_hour()}/h")
        print(f"the whole session cost about "
              f"${(time.time()-t0)/3600.0*r.price_per_hour():.2f}")
        print("=" * 72)
    finally:
        if r.pod_id:
            r.destroy()
            time.sleep(4)
            left = r._request("GET", "/pods").get("pods", [])
            print("pods running after the run:", [p.get("name") for p in left])
    return 0


if __name__ == "__main__":
    sys.exit(main())
