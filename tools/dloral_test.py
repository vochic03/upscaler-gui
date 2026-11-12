#!/usr/bin/env python3
"""Try DLoRAL on a rented card, on a few frames, and say what it costs.

    python3 tools/dloral_test.py --clip <file> --frames 20 --yes

The point is the measurement: a diffusion pass is slow and the number in
the estimate is a guess, so it has to be measured before anybody agrees to
run a film through it.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cloud  # noqa: E402
import keys  # noqa: E402
import rental  # noqa: E402

IMAGE = os.environ.get("UPSCALER_IMAGE_DLORAL", "docker.io/bzz1/upscaler:dloral")


def main():
    clip = None
    frames = 20
    scale = 4
    yes = False
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--clip" and i + 1 < len(args):
            clip = args[i + 1]
        elif a == "--frames" and i + 1 < len(args):
            frames = int(args[i + 1])
        elif a == "--scale" and i + 1 < len(args):
            scale = int(args[i + 1])
        elif a == "--yes":
            yes = True
    if not clip:
        print("give me a clip: --clip <file>", file=sys.stderr)
        return 1

    key = keys.resolve("runpod")
    if not key:
        print("no Runpod key in the keychain", file=sys.stderr)
        return 1

    work = os.path.join(os.path.dirname(os.path.abspath(clip)), "dloraltest")
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, "src.mp4")
    n, fps, w, h = cloud.probe_video(clip)
    cloud.split_video(clip, work, 0, min(frames, n), fps)
    # split_video names it after the first frame; find it
    cands = [f for f in os.listdir(work) if f.startswith("part-")]
    if not cands:
        print("could not cut the clip", file=sys.stderr)
        return 1
    src = os.path.join(work, sorted(cands)[0])

    r = rental.RunPod(key, gpu="NVIDIA L40S", image=IMAGE, engine="dloral",
                      name="dloral-test", disk=60, port=8000,
                      log=lambda m: print("  ", m, flush=True))
    print(f"the clip: {w}x{h} at {fps:.2f} fps, {frames} frames, {scale}x")
    print(f"the card: {r.gpu} at ${r.price_per_hour()}/h")
    if not yes:
        print("\nThis would rent a real GPU. Run it with --yes to go ahead.")
        return 1
    r.approve({"one_clip_cost": 1, "price_per_hour": r.price_per_hour()})
    t0 = time.time()
    try:
        r.create()
        endpoint = r.wait_ready(timeout=600)
        print(f"the worker answered after {time.time()-t0:.0f}s")
        out = os.path.join(work, "out.mp4")
        cloud.dloral_run(endpoint, src, out, scale=scale,
                         log=lambda m: print("  ", m, flush=True))
        took = time.time() - t0
        on_card = 0
        for p in os.listdir(os.path.join(work, "dloral-pieces")):
            pass
        print(f"\nRESULT: {out}")
        print(f"  the whole run took {took:.0f}s")
    finally:
        r.destroy()
        time.sleep(4)
        left = r._request("GET", "/pods").get("pods", [])
        print("pods running after the run:", [p.get("name") for p in left])
    return 0


if __name__ == "__main__":
    sys.exit(main())
