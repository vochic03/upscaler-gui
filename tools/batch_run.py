#!/usr/bin/env python3
"""Send a pile of videos through a Runpod Serverless endpoint.

The pod path is fine for one clip: the request goes through Cloudflare,
which gives it a hundred seconds, and a hundred seconds is a lot of
frames. A full-length film is more than that, and fifty films are more
than that again. That is what this is for.

The serverless endpoint takes a job as JSON and answers with JSON, so
there is no limit on how long a job may run. The batch API takes a whole
list of jobs at once, runs them on workers of their own, and hands back
the results when they are done.

    python3 tools/batch_run.py --endpoint <id> --outdir out/ clip*.mp4

Small clips go over as the video itself (the API allows about ten
megabytes a call). Anything bigger needs a URL the worker can fetch, so
give one with --url-template, with a %s where the file name goes.
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import keys  # noqa: E402

API = "https://api.runpod.ai/v2"
# The API takes about ten megabytes per call.
CHUNK_BYTES = 9 * 1024 * 1024


def call(method, path, key, body=None, params=""):
    url = f"{API}/{path}{params}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"{method} {path} -> {e.code}: {detail}")


def build_request(path, args):
    """One job. The video itself when it is small enough, a URL when not."""
    size = os.path.getsize(path)
    spec = {"scale": args.scale, "model": args.model, "engine": args.engine,
            "video_name": os.path.basename(path)}
    if args.denoise:
        spec["denoise"] = args.denoise
    if size <= CHUNK_BYTES:
        with open(path, "rb") as fh:
            spec["video"] = base64.b64encode(fh.read()).decode()
        return {"input": spec}
    if not args.url_template:
        raise RuntimeError(
            f"{os.path.basename(path)} is {size/1e6:.1f} MB, over the "
            f"{CHUNK_BYTES/1e6:.0f} MB the API takes in one call. Give a "
            "--url-template with a %s for the file name, and put the files "
            "somewhere the worker can fetch them.")
    spec["video_url"] = args.url_template % os.path.basename(path)
    return {"input": spec}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--endpoint", required=True, help="the serverless endpoint id")
    ap.add_argument("--videos", nargs="+", required=True)
    ap.add_argument("--outdir", default="work/batch")
    ap.add_argument("--scale", type=int, default=4)
    ap.add_argument("--model", default="upscayl-realesrgan-x4plus-v3")
    ap.add_argument("--engine", default="realesrgan")
    ap.add_argument("--denoise", default=None)
    ap.add_argument("--url-template", default=None,
                    help="https://host/path/%s for the big ones")
    ap.add_argument("--no-wait", action="store_true",
                    help="submit and leave; use --batch to pick it up later")
    args = ap.parse_args()

    key = keys.resolve("runpod")
    if not key:
        print("no Runpod key in the keychain", file=sys.stderr)
        return 1

    os.makedirs(args.outdir, exist_ok=True)

    # 1. a batch to put them in
    batch = call("POST", f"{args.endpoint}/batch", key, [])
    batch_id = batch["id"]
    print(f"batch {batch_id}")

    # 2. the jobs, in calls the API will take
    #
    # The docs show {"requests": [...]}, but the API wants a bare array -
    # it answers "cannot unmarshal object into Go value of type []*job.JobInput"
    # otherwise. The array is the same shape as the one that creates the batch.
    chunk = []
    size = 0
    sent = 0
    for path in args.videos:
        req = build_request(path, args)
        raw = len(json.dumps(req))
        if chunk and size + raw > CHUNK_BYTES:
            sent += len(chunk)
            print(f"  {sent}/{len(args.videos)} requests sent")
            call("POST", f"{args.endpoint}/batch/{batch_id}/requests", key, chunk)
            chunk, size = [], 0
        chunk.append(req)
        size += raw
    if chunk:
        sent += len(chunk)
        print(f"  {sent}/{len(args.videos)} requests sent")
        call("POST", f"{args.endpoint}/batch/{batch_id}/requests", key, chunk)

    # 3. and set it loose
    call("POST", f"{args.endpoint}/batch/{batch_id}/finalize", key)
    print(f"batch {batch_id} finalized")

    if args.no_wait:
        return 0

    # 4. wait for it, then collect the answers
    while True:
        info = call("GET", f"{args.endpoint}/batch/{batch_id}", key)
        done = info.get("requestCompleted", 0) + info.get("requestFailed", 0)
        print(f"  {done}/{info.get('requestTotal', 0)} done "
              f"({info.get('requestFailed', 0)} failed)")
        if info.get("requestTotal") and done >= info["requestTotal"]:
            break
        time.sleep(10)

    offset = 0
    got = 0
    while True:
        page = call("GET", f"{args.endpoint}/batch/{batch_id}/requests", key, None,
                    f"?offset={offset}&limit=50")
        for req in page.get("requests", []):
            if req.get("status") != "COMPLETED":
                if req.get("error"):
                    print(f"  failed: {req['error'][:160]}")
                continue
            out = req.get("output") or {}
            if out.get("video"):
                name = os.path.splitext(os.path.basename(req.get("input", {}).get(
                    "video_name", f"result-{got}.mp4")))[0] + ".mp4"
                dest = os.path.join(args.outdir, name)
                with open(dest, "wb") as fh:
                    fh.write(base64.b64decode(out["video"]))
                got += 1
                print(f"  wrote {dest} ({out.get('bytes', 0)/1e6:.1f} MB, "
                      f"{out.get('seconds')}s on the card)")
            elif out.get("s3_key"):
                print(f"  in the bucket: {out['s3_key']}")
        offset += len(page.get("requests", []))
        if not page.get("hasMore"):
            break
    print(f"done: {got} files in {args.outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
