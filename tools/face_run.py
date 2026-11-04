#!/usr/bin/env python3
"""Repair the faces in a video, on a rented GPU - in one session.

  estimate -> rent -> probe -> measure -> send the film in pieces -> join ->
  destroy the pod

The probe is 30 frames: it proves the card works and gives the real rate,
which decides how big the pieces can be. The pieces have to fit inside
Cloudflare's hundred seconds, so the rate decides how many requests the film
costs.

Nothing is rented until the estimate has been printed. The pod is destroyed
in a finally, so a failure still gives the machine back.

    python3 tools/face_run.py --path <video>            # the estimate only
    python3 tools/face_run.py --path <video> --yes      # go ahead
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cloud  # noqa: E402
import keys  # noqa: E402
import rental  # noqa: E402

IMAGE = os.environ.get("UPSCALER_IMAGE", "docker.io/bzz1/upscaler:latest")
# A piece may not take longer than this, because the proxy gives a request
# about a hundred seconds.
PIECE_BUDGET_SECONDS = 60


def probe(endpoint, path, fps, out_dir, log):
    """Thirty frames, to see what the card does."""
    part = cloud.split_video(path, out_dir, 0, 30, fps)
    out = os.path.join(out_dir, "probe-out.mp4")
    t0 = time.time()
    cloud.upscale(endpoint, part, out, route="/face", log=log)
    took = time.time() - t0
    log(f"the probe took {took:.0f}s for 30 frames")
    return took


def main():
    # The face pass is switched off. It was tried on the music video and the
    # result looked unnatural next to the Topaz reference, so it is not to
    # be used. The code stays (the image is already built; rebuilding it to
    # remove a route would cost more than it saves) but nothing calls it.
    if not os.environ.get("UPSCALER_ALLOW_FACE_PASS"):
        print("the face pass is switched off - it was rejected as unnatural "
              "next to the Topaz reference. Set UPSCALER_ALLOW_FACE_PASS=1 "
              "if you ever want to look at it again.", file=sys.stderr)
        return 1
    path = out = None
    yes = False
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--path" and i + 1 < len(args):
            path = args[i + 1]
        elif a == "--out" and i + 1 < len(args):
            out = args[i + 1]
        elif a == "--yes":
            yes = True
    if not path:
        print("give me a video: --path <file>", file=sys.stderr)
        return 1
    outdir = os.path.join(os.path.dirname(os.path.abspath(path)), "facework")
    if not out:
        stem = os.path.splitext(os.path.basename(path))[0]
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "work", "output", stem + "-faces.mp4")
    out = os.path.abspath(out)
    os.makedirs(outdir, exist_ok=True)

    key = keys.resolve("runpod")
    if not key:
        print("no Runpod key in the keychain", file=sys.stderr)
        return 1

    frames, fps, w, h = cloud.probe_video(path)
    r = rental.RunPod(key, gpu="NVIDIA GeForce RTX 4090", image=IMAGE,
                      name="upscaler-faces", disk=60, port=8000,
                      log=lambda m: print("  ", m, flush=True))

    # The estimate, before anything is spent. The rate is a guess until the
    # probe says otherwise; the real number comes out of the run.
    guess = 8.0
    plan = {
        "gpu": r.gpu,
        "price_per_hour": r.price_per_hour(),
        "frames": frames,
        "seconds_of_video": round(frames / fps, 1) if fps else 0,
        "fps_on_the_card": guess,
        "ai_seconds": round(frames / guess),
        "spin_up_seconds": rental.POD_OVERHEAD_SECONDS,
        "one_clip_cost": round((frames / guess + rental.POD_OVERHEAD_SECONDS)
                               / 3600.0 * r.price_per_hour(), 4),
        "image": IMAGE,
    }
    print("ESTIMATE")
    for k in ("gpu", "price_per_hour", "frames", "seconds_of_video",
              "fps_on_the_card", "ai_seconds", "spin_up_seconds",
              "one_clip_cost", "image"):
        print(f"   {k:20} {plan.get(k)}")
    if not yes:
        print("\nThis would rent a real GPU. Run it with --yes to go ahead.")
        return 1

    r.approve(plan)
    t0 = time.time()
    try:
        r.create()
        endpoint = r.wait_ready(timeout=600)
        print(f"the worker answered at {endpoint} after {time.time()-t0:.0f}s")

        # 1. the probe: does it work, and how fast
        took = probe(endpoint, path, fps, outdir, lambda m: print("  ", m, flush=True))
        rate = 30.0 / max(1.0, took)
        print(f"  MEASURED: {rate:.1f} frames a second on the card")

        # 2. the pieces, sized by that rate so each one stays inside the
        #    proxy's hundred seconds with room for the transfer
        chunk = int(rate * PIECE_BUDGET_SECONDS)
        chunk = max(60, min(chunk, 2000))
        pieces = len(range(0, frames, chunk))
        est = (frames / rate + rental.POD_OVERHEAD_SECONDS) / 3600.0 * r.price_per_hour()
        print(f"  {pieces} pieces of up to {chunk} frames; "
              f"the whole run should cost about ${est:.3f}")

        # 3. the film
        cloud.face_chunked(endpoint, path, out, chunk_frames=chunk,
                           log=lambda m: print("  ", m, flush=True))
        print(f"RESULT: {out} ({os.path.getsize(out)/1e6:.1f} MB)")
    finally:
        r.destroy()
        time.sleep(4)
        left = r._request("GET", "/pods").get("pods", [])
        print("pods running after the run:", [p.get("name") for p in left])
    print(f"the whole thing took {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
