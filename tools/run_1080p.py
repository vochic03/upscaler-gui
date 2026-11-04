#!/usr/bin/env python3
"""A compressed capture, back to 1080p, on a rented card.

The picture is already 1920x1080, so nothing needs enlarging - what is
missing is the detail the low bitrate threw away. So the AI pass works on a
half-size canvas (where the blocking has already been averaged away) and
its result is scaled back down to 1080p. That is the "enhance, then
rescale" way round, and it is both cheaper and cleaner than enlarging.

    python3 tools/run_1080p.py --canvas <canvas> --original <denoised> --yes
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cloud  # noqa: E402
import keys  # noqa: E402
import rental  # noqa: E402

IMAGE = os.environ.get("UPSCALER_IMAGE", "docker.io/bzz1/upscaler:latest")
MODEL = os.environ.get("UPSCALER_MODEL", "realesr-general-wdn-x4v3")
SCALE = int(os.environ.get("UPSCALER_SCALE", "4"))
CHUNK = int(os.environ.get("UPSCALER_CHUNK", "200"))
TARGET = (1920, 1080)


def main():
    canvas = original = out = None
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--canvas" and i + 1 < len(args):
            canvas = args[i + 1]
        elif a == "--original" and i + 1 < len(args):
            original = args[i + 1]
        elif a == "--out" and i + 1 < len(args):
            out = args[i + 1]
    yes = "--yes" in args
    if not canvas or not original:
        print("give me --canvas and --original", file=sys.stderr)
        return 1
    if not out:
        out = os.path.join(os.path.dirname(os.path.abspath(canvas)),
                           "..", "..", "work", "output",
                           os.path.splitext(os.path.basename(canvas))[0] + "-1080p.mp4")
    out = os.path.abspath(out)
    work = os.path.join(os.path.dirname(out), "aiwork")
    os.makedirs(work, exist_ok=True)

    frames, fps, w, h = cloud.probe_video(canvas)
    pixels = w * h
    seconds = frames / (fps or 25)
    key = keys.resolve("runpod")
    r = rental.RunPod(key, gpu="NVIDIA GeForce RTX 4090", image=IMAGE,
                      name="upscaler-1080p", disk=60, port=8000,
                      log=lambda m: print("  ", m, flush=True))
    price = r.price_per_hour()
    ai_seconds = frames * pixels / (4.5e6)
    print(f"the canvas: {w}x{h}, {frames} frames ({seconds:.0f}s of film)")
    print(f"the card:   {r.gpu} at ${price}/h")
    print(f"the AI:     about {ai_seconds:.0f}s of model time "
          f"({frames * pixels / 4.5e6 / 60:.1f} min)")
    print(f"the cost:   about ${(ai_seconds + 240) / 3600.0 * price:.3f} "
          f"with the pod start-up")
    if not yes:
        print("\nThis would rent a real GPU. Run it with --yes to go ahead.")
        return 1

    r.approve({"one_clip_cost": (ai_seconds + 240) / 3600.0 * price,
               "price_per_hour": price})
    t0 = time.time()
    ai = os.path.join(work, "ai.mp4")
    # The pool runs out of cards. Ask for the fast one, and take whatever
    # is actually there if it is not.
    candidates = [os.environ.get("UPSCALER_GPU", "NVIDIA GeForce RTX 4090"),
                  "NVIDIA GeForce RTX 3090 Ti",
                  "NVIDIA GeForce RTX 3090",
                  "NVIDIA RTX A5000"]
    try:
        endpoint = None
        for gpu in candidates:
            r.gpu = gpu
            try:
                r.create()
                endpoint = r.wait_ready(timeout=600)
                print(f"the card: {gpu} at ${r.price_per_hour()}/h")
                break
            except Exception as e:
                print(f"  no {gpu}: {str(e)[:120]}")
                r.pod_id = None
        if not endpoint:
            raise RuntimeError("no card was available")
        print(f"the worker answered after {time.time()-t0:.0f}s")
        cloud.upscale_chunked(endpoint, canvas, ai, chunk_frames=CHUNK,
                              scale=SCALE, model=MODEL,
                              log=lambda m: print("  ", m, flush=True))
    finally:
        if r.pod_id:
            r.destroy()
            time.sleep(4)
            left = r._request("GET", "/pods").get("pods", [])
            print("pods running after the run:", [p.get("name") for p in left])
    card = time.time() - t0
    print(f"the card took {card:.0f}s")

    # The finishing, here: the AI result scaled back to the target, blended
    # with the plain enlargement of the denoised original, then softened,
    # grained, graded and encoded.
    import engine
    look = dict(engine.LOOKS["polished"])
    t1 = time.time()
    cloud.finish_locally(ai, original, out, scale=SCALE, look=look,
                         encoder="h264", target=TARGET,
                         log=lambda m: print("  ", m, flush=True))
    print(f"finished in {time.time()-t1:.0f}s -> {out} "
          f"({os.path.getsize(out)/1e6:.1f} MB)")
    print("RESULT", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
