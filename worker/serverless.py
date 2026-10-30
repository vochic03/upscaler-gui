#!/usr/bin/env python3
"""The worker as a Runpod Serverless endpoint.

Same work, different door. A serverless worker is handed a job as JSON
and answers with JSON, so there is no hundred-second cap on the request:
a whole film can go through in one job, and a hundred films can go
through as a hundred jobs.

    POST /        {"input": {...}}    ->  {"output": {...}}
    GET  /health                      ->  {"status": "ok"}

The input is either the video itself as base64 (the API allows about ten
megabytes) or a URL to fetch it from - for anything bigger, use the URL.
The answer is the video as base64 when it fits, or - when the job carries
storage details - the key it was written to.

On Runpod the job does not arrive over HTTP: the worker *pulls* it from
RUNPOD_WEBHOOK_GET_JOB and posts the answer back to
RUNPOD_WEBHOOK_POST_OUTPUT. That is the runpod SDK's job, so on Runpod
this hands the work to the SDK, and the HTTP server below is for running
the same thing locally.

The work itself is the same code the pod runs (server.process), so the
two cannot drift apart.
"""
import base64
import json
import os
import shutil
import tempfile
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import server as worker

PORT = int(os.environ.get("RUNPOD_HTTP_PORT", os.environ.get("PORT", "8000")))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# The names Runpod uses for the storage details in the environment. The
# job body is the documented place; these are the fallback.
S3_ENV = {
    "accessId": ("RUNPOD_S3_ACCESS_ID", "RUNPOD_S3_ACCESS_KEY"),
    "accessSecret": ("RUNPOD_S3_SECRET_ACCESS_KEY", "RUNPOD_S3_SECRET_KEY"),
    "bucketName": ("RUNPOD_S3_BUCKET_NAME", "RUNPOD_S3_BUCKET"),
    "endpointUrl": ("RUNPOD_S3_ENDPOINT_URL", "RUNPOD_S3_ENDPOINT"),
}


def s3_from_env():
    out = {}
    for key, names in S3_ENV.items():
        for name in names:
            if os.environ.get(name):
                out[key] = os.environ[name]
                break
    return out if len(out) == len(S3_ENV) else None


def fetch(url, dest):
    """Pull a file down. The pod's own DNS is broken by default, so the
    resolver is pointed at a public one first."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=1800) as r:
        with open(dest, "wb") as fh:
            shutil.copyfileobj(r, fh)


def put_to_s3(path, s3, key):
    """Write the result to the bucket the job named."""
    try:
        import boto3
    except ImportError:
        return None
    client = boto3.client("s3", aws_access_key_id=s3["accessId"],
                          aws_secret_access_key=s3["accessSecret"],
                          endpoint_url=s3.get("endpointUrl") or None)
    client.upload_file(path, s3["bucketName"], key)
    return key


def run_job(spec, s3=None):
    """Do one job, and say what came of it."""
    work = tempfile.mkdtemp(prefix="job-")
    try:
        src = os.path.join(work, "in.mp4")
        if spec.get("video"):
            raw = spec["video"]
            if raw.startswith("data:"):
                raw = raw.split(",", 1)[-1]
            with open(src, "wb") as fh:
                fh.write(base64.b64decode(raw))
        elif spec.get("video_url"):
            fetch(spec["video_url"], src)
        else:
            raise RuntimeError("the job has no video and no video_url")

        out = os.path.join(work, "out.mp4")
        t0 = time.time()
        worker.process(src, out,
                       scale=int(spec.get("scale") or 4),
                       model=spec.get("model"),
                       denoise=spec.get("denoise"),
                       engine=(spec.get("engine") or "realesrgan").lower(),
                       prompt=spec.get("prompt") or "",
                       resolution=int(spec.get("resolution") or 1080))
        size = os.path.getsize(out)
        seconds = round(time.time() - t0, 1)
        print(f"job done: {size/1e6:.1f} MB in {seconds}s", flush=True)

        result = {"bytes": size, "seconds": seconds,
                  "model": spec.get("model"), "scale": spec.get("scale")}
        key = None
        if s3:
            key = spec.get("s3_key") or ("out-%d.mp4" % int(time.time() * 1000))
            try:
                put_to_s3(out, s3, key)
                result["s3_key"] = key
                result["bucket"] = s3["bucketName"]
            except Exception as e:
                print(f"could not write to the bucket: {e}", flush=True)
        if not key:
            with open(out, "rb") as fh:
                result["video"] = base64.b64encode(fh.read()).decode()
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def handler(event):
    """The Runpod handler. The job is in event["input"]."""
    spec = event.get("input") or {}
    s3 = event.get("s3Config") or s3_from_env()
    return run_job(spec, s3)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"{time.strftime('%H:%M:%S')}  {fmt % args}", flush=True)

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", ""):
            self._json({"status": "ok", "gpu": worker.gpu_name(),
                        "backend": "cuda" if worker.CUDA else "ncnn"})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8", "replace"))
        except Exception as e:
            self._json({"error": f"the job is not JSON: {e}"}, 400)
            return
        spec = body.get("input") or {}
        if not spec:
            self._json({"error": "the job has no input"}, 400)
            return
        s3 = body.get("s3Config") or s3_from_env()
        try:
            self._json({"output": run_job(spec, s3)})
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def main():
    # On Runpod the job is pulled, not posted, so the SDK does the talking.
    if os.environ.get("RUNPOD_WEBHOOK_GET_JOB"):
        import runpod
        print(f"upscaler serverless on the SDK "
              f"(gpu={worker.gpu_name()})", flush=True)
        runpod.serverless.start({"handler": handler})
        return
    # Locally there is nobody to pull from, so the same job is served over
    # plain HTTP - the same shape the API uses.
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"upscaler serverless on :{PORT}  gpu={worker.gpu_name()}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
