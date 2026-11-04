#!/usr/bin/env python3
"""
Local helper for the DRM capture player.

  * serves the static app from this directory
  * POST /proxy  {"url": ..., "headers": {...}}  -> raw upstream bytes
  * POST /dump   {"name": ..., "data": "<base64>"} -> writes a file to ./out

Why the proxy exists:
  - CDNs that serve DRM manifests/segments usually do not send
    Access-Control-Allow-Origin, so the page cannot fetch them directly.
  - Those URLs normally require the session cookies and one-time tokens that
    WidevineProxy2 captured from the live player. The proxy replays them.

It binds to 127.0.0.1 only and is meant to be run locally.

    python3 proxy.py [--port 8765]
"""

import argparse
import base64
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "out")

# Headers that must not be forwarded verbatim: they either describe the
# original TCP connection or would make urllib return a compressed body that
# it cannot decode.
DROP_HEADERS = {
    "accept-encoding",
    "connection",
    "content-length",
    "host",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "sec-fetch-user",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "upgrade-insecure-requests",
    "proxy-connection",
    "te",
}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    # --- helpers ---------------------------------------------------------

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Expose-Headers", "*")

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return {}

    def log_message(self, fmt, *args):  # quieter output
        if "--verbose" in sys.argv:
            super().log_message(fmt, *args)

    def end_headers(self):
        # Never cache: the app and the fixtures change while you iterate.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    # --- routes ----------------------------------------------------------

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.path.startswith("/proxy"):
            self.handle_proxy()
        elif self.path.startswith("/dump"):
            self.handle_dump()
        else:
            self._json({"error": "not found"}, 404)

    def handle_proxy(self):
        payload = self._read_json()
        url = payload.get("url")
        if not url:
            self._json({"error": "missing url"}, 400)
            return
        headers = {}
        for k, v in (payload.get("headers") or {}).items():
            if k.lower() in DROP_HEADERS:
                continue
            headers[k] = v

        req = urllib.request.Request(url, headers=headers, method=payload.get("method", "GET"))
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type", "application/octet-stream")
                self.send_response(resp.status)
                self._cors()
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            body = e.read()
            self.send_response(e.code)
            self._cors()
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"{type(e).__name__}: {e}"}, 502)

    def handle_dump(self):
        payload = self._read_json()
        name = os.path.basename(payload.get("name") or "dump.bin")
        os.makedirs(OUT_DIR, exist_ok=True)
        data = base64.b64decode(payload.get("data") or "")
        with open(os.path.join(OUT_DIR, name), "wb") as fh:
            fh.write(data)
        self._json({"ok": True, "bytes": len(data), "path": os.path.join(OUT_DIR, name)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("application/xml", ".xml")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"DRM capture player: http://{args.host}:{args.port}/")
    print(f"proxy endpoint:     POST http://{args.host}:{args.port}/proxy")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
