#!/usr/bin/env python3
"""Time the two ways of getting a clean 1080p out of a compressed capture.

    python3 tools/measure_paths.py --yes

One pod session, both paths, so the comparison costs one spin-up:

  * the diffusion (DLoRAL) at 2x from a half-size canvas - what the
    "Regenerate" preset does;
  * Real-ESRGAN with the weak-denoise model, which is the model trained
    for compression artefacts.

The numbers are per frame, so the cost of a whole film follows from them.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cloud  # noqa: E402
import keys  # noqa: E402
import rental  # noqa: E402

IMAGE = os.environ.get("UPSCALER_IMAGE_DLORAL", "docker.io/bzz1/upscaler:dloral")
WORK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "work", "dloraltest")


def time_upscale(endpoint, path, out, model, scale):
    t0 = time.time()
    cloud.upscale(endpoint, path, out, scale=scale, model=model,
                  log=lambda m: None)
    return time.time() - t0


def main():
    yes = "--yes" in sys.argv
    half = os.path.join(WORK, "half.mp4")     # 960x540, denoised
    full = os.path.join(WORK, "full.mp4")     # 1920x1080
    if not os.path.exists(half):
        print("make the test clips first", file=sys.stderr)
        return 1
    frames, fps, w, h = cloud.probe_video(half)
    key = keys.resolve("runpod")
    r = rental.RunPod(key, gpu="NVIDIA L40S", image=IMAGE, engine="dloral",
                      name="measure-paths", disk=60, port=8000,
                      log=lambda m: print("  ", m, flush=True))
    print(f"the test clip: {frames} frames of {w}x{h}")
    if not yes:
        print("\nThis would rent a real GPU. Run it with --yes to go ahead.")
        return 1
    r.approve({"one_clip_cost": 1, "price_per_hour": r.price_per_hour()})
    t0 = time.time()
    try:
        r.create()
        endpoint = r.wait_ready(timeout=600)
        print(f"the worker answered after {time.time()-t0:.0f}s")

        # 1. the diffusion, 2x from the half-size canvas -> 1080p
        out1 = os.path.join(WORK, "dloral2x.mp4")
        t1 = time.time()
        try:
            cloud.dloral_run(endpoint, half, out1, scale=2,
                             log=lambda m: print("   ", m, flush=True))
            d1 = time.time() - t1
            print(f"DIFFUSION 2x: {d1:.0f}s for {frames} frames = "
                  f"{frames/d1:.2f} frames/s -> a 3615 frame film would take "
                  f"{3615/(frames/d1)/3600:.1f} h")
        except Exception as e:
            print("DIFFUSION 2x FAILED:", str(e)[:600])

        # 2. the diffusion at 4x from the half canvas (the trained factor)
        out2 = os.path.join(WORK, "dloral4x.mp4")
        t2 = time.time()
        try:
            cloud.dloral_run(endpoint, half, out2, scale=4,
                             log=lambda m: print("   ", m, flush=True))
            d2 = time.time() - t2
            print(f"DIFFUSION 4x: {d2:.0f}s for {frames} frames = "
                  f"{frames/d2:.2f} frames/s -> a 3615 frame film would take "
                  f"{3615/(frames/d2)/3600:.1f} h")
        except Exception as e:
            print("DIFFUSION 4x FAILED:", str(e)[:400])

        # 3. the weak-denoise Real-ESRGAN, 4x from the half canvas
        out3 = os.path.join(WORK, "wdn.mp4")
        d3 = time_upscale(endpoint, half, out3, "realesr-general-wdn-x4v3", 4)
        print(f"REAL-ESRGAN WDN 4x: {d3:.0f}s for {frames} frames = "
              f"{frames/d3:.2f} frames/s -> a 3615 frame film would take "
              f"{3615/(frames/d3)/3600:.1f} h")

        # 4. the compact model at 2x, straight from 1080p
        out4 = os.path.join(WORK, "compact2x.mp4")
        d4 = time_upscale(endpoint, full, out4,
                           "xintao-realesr-animevideo-x2-v3", 2)
        print(f"REAL-ESRGAN compact 2x (from 1080p): {d4:.0f}s for {frames} "
              f"frames = {frames/d4:.2f} frames/s -> a 3615 frame film would "
              f"take {3615/(frames/d4)/3600:.1f} h")
    finally:
        r.destroy()
        time.sleep(4)
        left = r._request("GET", "/pods").get("pods", [])
        print("pods running after the run:", [p.get("name") for p in left])
    return 0


if __name__ == "__main__":
    sys.exit(main())
