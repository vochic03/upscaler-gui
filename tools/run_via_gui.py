#!/usr/bin/env python3
"""Run one job through the local GUI's own API and follow it.

    python3 tools/run_via_gui.py --path <video> --preset cloud [--dry-run]

It asks the GUI for the estimate, shows it, and only then starts the job.
Nothing is rented without that estimate in front of us.
"""
import argparse
import json
import os
import sys
import time
import urllib.request

GUI = "http://127.0.0.1:8790"


def call(method, path, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GUI + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", required=True)
    ap.add_argument("--preset", default="cloud")
    ap.add_argument("--model", default="xintao-realesr-animevideo-x2-v3")
    ap.add_argument("--scale", type=int, default=2)
    ap.add_argument("--look", default="polished")
    ap.add_argument("--denoise-engine", default="bilateral")
    ap.add_argument("--encoder", default="h264")
    ap.add_argument("--max-resolution", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--approve", action="store_true",
                    help="go ahead and rent once the estimate is printed")
    args = ap.parse_args()

    body = {
        "path": os.path.abspath(args.path),
        "backend": "cloud" if args.preset == "cloud" else args.preset,
        "cloud_engine": "realesrgan",
        "model": args.model,
        "scale": args.scale,
        "look": args.look,
        "denoise_engine": args.denoise_engine,
        "bilateral_radius": 7,
        "bilateral_strength": 0.85,
        "encoder": args.encoder,
        "max_resolution": args.max_resolution,
        "deinterlace": "auto",
        "crop": "",
        "motion": 0,
        "rent": True,
        "rent_provider": "runpod",
    }
    estimate = call("POST", "/api/estimate", body)
    print("THE ESTIMATE")
    for k in ("gpu", "price_per_hour", "frames", "seconds_of_video", "width",
              "height", "mpx_per_second", "measured", "ai_seconds",
              "spin_up_seconds", "one_clip_seconds", "one_clip_cost", "image"):
        print(f"   {k:20} {estimate.get(k)}")
    print(f"   {'with the chunking':20} about "
          f"${estimate.get('one_clip_cost', 0) * 1.12:.3f} "
          f"(the pieces add a minute of transfer)")
    if args.dry_run:
        print("\ndry run - nothing was rented")
        return 0
    if not args.approve:
        print("\nnot approved - pass --approve to rent")
        return 1

    # --approve is the user saying yes at the terminal. The server only
    # takes a yes that carries the token from its own page, so this reads
    # it from there - the same thing the GUI does. (Writing
    # "rent_approved": true into the job is not an approval and the server
    # throws it away.)
    import re
    import urllib.request
    with urllib.request.urlopen(GUI + "/", timeout=20) as r:
        m = re.search(r'name="approve-token" content="([0-9a-f]+)"',
                      r.read().decode("utf-8", "replace"))
    req = urllib.request.Request(
        GUI + "/api/approve",
        data=json.dumps({"id": estimate.get("approval_id"), "yes": True}).encode(),
        headers={"Content-Type": "application/json",
                 "X-Approve-Token": m.group(1) if m else ""}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        print("approved:", json.loads(r.read().decode()).get("state"))
    body["rent_approval"] = estimate.get("approval_id")
    job = call("POST", "/api/run", body)
    jid = job.get("id")
    print(f"\njob {jid} started")
    seen = 0
    t0 = time.time()
    while True:
        time.sleep(10)
        j = call("GET", f"/api/job?id={jid}")
        log = j.get("log") or []
        for line in log[seen:]:
            print("   ", line)
        seen = len(log)
        print(f"    [{time.time()-t0:6.0f}s] {j.get('state')} - {j.get('stage')} "
              f"({j.get('progress', 0)*100:.0f}%) {j.get('message') or ''}")
        if j.get("state") in ("done", "failed", "cancelled"):
            print("\nstate:", j.get("state"))
            if j.get("error"):
                print("error:", j.get("error"))
            if j.get("output"):
                print("output:", j.get("output"))
            return 0 if j.get("state") == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
