#!/usr/bin/env python3
"""The whole loop, end to end, against a real rented GPU.

  estimate -> approve -> rent -> wait for the worker -> send the clip ->
  get the upscale back -> destroy the pod

Nothing is rented until the estimate has been printed and the run has been
approved. The pod is destroyed in a finally, so a failure still gives the
machine back.
"""
import os
import sys
import time

sys.path.insert(0, "/Users/vasilijemicic/upscaler-gui")
import cloud
import keys
import rental

CLIP = "/Users/vasilijemicic/upscaler-gui/work/collage/clip.mp4"
IMAGE = "docker.io/bzz1/upscaler:latest"
OUT = "/Users/vasilijemicic/upscaler-gui/work/output/e2e-rented.mp4"


def main():
    key = keys.resolve("runpod")
    r = rental.RunPod(key, gpu="NVIDIA GeForce RTX 4090", image=IMAGE,
                      name="upscaler-e2e", disk=40, port=8000,
                      log=lambda m: print("  ", m, flush=True))

    # 1. the estimate, before anything is spent
    plan = r.plan(frames=300, fps=25.0, engine="realesrgan")
    print("ESTIMATE")
    for k in ("gpu", "price_per_hour", "frames", "seconds_of_video",
              "fps_on_the_card", "ai_seconds", "spin_up_seconds",
              "one_clip_seconds", "one_clip_cost"):
        print(f"   {k:20} {plan.get(k)}")
    print("   a batch in one session:",
          [(b["clips"], "$" + str(b["cost"])) for b in plan.get("batches", [])])
    print("   the image:", plan.get("image"))

    if "--yes" not in sys.argv:
        print("\nThis would rent a real GPU. Run it with --yes to go ahead.")
        return

    # 2. approve, rent, work, and give it back
    r.approve(plan)
    t0 = time.time()
    try:
        r.create()
        endpoint = r.wait_ready(timeout=600)
        print(f"the worker answered at {endpoint} after "
              f"{time.time()-t0:.0f}s")
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        cloud.upscale(endpoint, CLIP, OUT, scale=4,
                      model="upscayl-realesrgan-x4plus-v3",
                      engine="realesrgan",
                      log=lambda m: print("  ", m, flush=True))
        size = os.path.getsize(OUT)
        print(f"RESULT: {OUT} ({size/1e6:.1f} MB)")
    finally:
        r.destroy()
        time.sleep(4)
        left = r._request("GET", "/pods").get("pods", [])
        print("pods running after the run:",
              [p.get("name") for p in left])
    print(f"the whole thing took {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
