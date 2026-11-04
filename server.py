#!/usr/bin/env python3
"""
A small local GUI for Holloway's Upscaler.

    python3 server.py [--port 8790]

Binds to 127.0.0.1 only. Everything it exposes is on this machine.
"""

import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import engine

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "work")
JOBS_DIR = os.path.join(WORK, "jobs")
UPLOADS = os.path.join(WORK, "uploads")
OUTBOX = os.path.join(WORK, "output")

# Folders the file browser may look in. Keeping this list tight means the
# server cannot be used to read arbitrary files on the machine.
LIBRARY = [
    os.path.expanduser("~/Videos"),
    os.path.expanduser("~/Movies"),
    os.path.expanduser("~/Downloads"),
    os.path.expanduser("~/Desktop"),
    os.path.expanduser("~/drm-capture/out"),
    os.path.expanduser("~/drm-capture"),
    os.path.dirname(os.path.expanduser("~/drm-capture/out")),
]
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".wmv", ".flv"}

JOBS = {}

# The approvals.
#
# A job that rents a GPU needs the user's approval of the estimate, and
# the approval has to come from the user's side. An agent, a script or
# anything else that can talk to this server must not be able to approve
# its own spending: /api/estimate therefore issues a PENDING approval
# with the price, the GUI (or the user at the terminal) approves it, and
# only then can the job that carries its id run.
#
# The approval is bound to what it was priced for: the clip, the engine
# and the card. Change any of them and it no longer matches.
APPROVALS = {}

# What the agent interface is doing, and what it said.
AGENTS = {"installed": False, "log": "", "busy": False, "agents": []}


def agents_state():
    """The state of the API Anything interface."""
    import tools.api_anything as aa
    spec = os.path.join(aa.home(), "sites", aa.SITE + ".json")
    out = dict(AGENTS)
    out["installed"] = os.path.isfile(spec)
    out["spec"] = spec
    out["mcp"] = aa.MCP
    return out


def agents_install(log=None):
    """Connect the agents. It runs npx, so it takes a while."""
    import tools.api_anything as aa
    AGENTS["busy"] = True
    AGENTS["log"] = "connecting..."
    try:
        aa.install_spec()
        code, out = aa.npx("setup", "--no-share")
        AGENTS["log"] = (out or "").strip()[-1200:] or "done"
        AGENTS["installed"] = True
    except Exception as e:
        AGENTS["log"] = "could not connect them: " + str(e)[:200]
    finally:
        AGENTS["busy"] = False
    return AGENTS["log"]


APPROVAL_TTL = 900          # fifteen minutes


def new_approval(estimate, path="", engine_name=""):
    """A pending approval for a price. Nobody has agreed to it yet."""
    aid = uuid.uuid4().hex[:12]
    APPROVALS[aid] = {
        "id": aid,
        "state": "pending",
        "created": time.time(),
        "expires": time.time() + APPROVAL_TTL,
        "path": path,
        "engine": engine_name,
        "gpu": (estimate or {}).get("gpu"),
        "cost": (estimate or {}).get("one_clip_cost"),
        "cold_cost": (estimate or {}).get("first_run_cost"),
        "estimate": estimate,
    }
    return aid


def approval_matches(a, body):
    """Is this approval the consent for THIS job?"""
    if not a:
        return False, "there is no such approval"
    if a["state"] != "approved":
        return False, ("this approval is " + a["state"] +
                       " - the user has to approve it first")
    if a["expires"] < time.time():
        a["state"] = "expired"
        return False, "this approval has expired"
    if (a.get("path") or "") != (body.get("path") or ""):
        return False, "this approval is for another clip"
    eng = body.get("cloud_engine") or ""
    if (a.get("engine") or "") != eng:
        return False, "this approval is for another engine"
    gpu = (body.get("rent_gpu") or "").strip()
    if gpu and a.get("gpu") and gpu != a["gpu"]:
        return False, "this approval is for another card"
    return True, ""



MEDIA_SUFFIXES = (".mp4", ".webm", ".mov", ".m4v", ".mkv", ".mpd", ".m3u8")


def _media_urls_in(obj, out=None, depth=0):
    """Every URL in an answer that looks like a video."""
    if out is None:
        out = []
    if depth > 6:
        return out
    if isinstance(obj, str):
        low = obj.lower().split("?")[0]
        if low.endswith(MEDIA_SUFFIXES):
            out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _media_urls_in(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _media_urls_in(v, out, depth + 1)
    return out


def fetch_video(url, log=print):
    """A video from a URL.

    A direct link is simply downloaded. A site with no API of its own is
    asked about through API Anything - the same way an agent would reach
    it: find an operation for the site, call it, and take the video URL
    out of the answer.
    """
    import subprocess as sp
    import urllib.parse
    if not url.lower().startswith(("http://", "https://")):
        raise RuntimeError("that is not a URL")
    dest = os.path.join(UPLOADS, "fetch-" + uuid.uuid4().hex[:8] + ".mp4")
    log("downloading " + url)
    r = sp.run(["curl", "-fsSL", "--max-time", "900", "--max-filesize",
                "2147483648", "-o", dest, url],
               stdout=sp.PIPE, stderr=sp.PIPE)
    if r.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 4096:
        # It downloaded something - but a page is not a video, and the
        # answer to a page is in its API, not in the page itself.
        try:
            engine.video_info(dest)
            log("got it: %.1f MB" % (os.path.getsize(dest) / 1e6))
            return dest
        except Exception:
            log("that was a page, not a video")
    # Not a file: ask API Anything what this site can give.
    try:
        os.remove(dest)
    except OSError:
        pass
    host = urllib.parse.urlparse(url).netloc
    log("that was not a file - asking API Anything about " + host)
    import tools.api_anything as aa
    code, out = aa.npx("search", host, timeout=120)
    if code != 0:
        raise RuntimeError("API Anything could not look up " + host +
                           ": " + (out or "").strip()[:200])
    try:
        found = json.loads(out)
    except Exception:
        raise RuntimeError("API Anything did not answer about " + host)
    matches = [m for m in (found.get("matches") or [])
               if m.get("site") and m.get("installed") is not False]
    for m in matches:
        for op in (m.get("operations") or []):
            code, out = aa.npx("call", m["site"], op["name"],
                               timeout=300)
            if code != 0:
                continue
            try:
                data = json.loads(out)
            except Exception:
                continue
            videos = _media_urls_in(data.get("data"))
            if not videos:
                continue
            log("found it through " + m["site"] + " " + op["name"])
            r = sp.run(["curl", "-fsSL", "--max-time", "900",
                        "--max-filesize", "2147483648", "-o", dest, videos[0]],
                       stdout=sp.PIPE, stderr=sp.PIPE)
            if r.returncode == 0 and os.path.exists(dest)                     and os.path.getsize(dest) > 4096:
                return dest
    raise RuntimeError("no video could be found at " + url)



def ensure_dirs():
    for d in (WORK, JOBS_DIR, UPLOADS, OUTBOX):
        os.makedirs(d, exist_ok=True)


def allowed_path(path):
    """True when the path is inside one of the allowed roots."""
    p = os.path.realpath(os.path.abspath(path))
    for root in LIBRARY + [UPLOADS, OUTBOX, WORK]:
        r = os.path.realpath(os.path.abspath(root))
        if p == r or p.startswith(r + os.sep):
            return True
    return False


def scan_library():
    out = []
    seen = set()
    for root in LIBRARY:
        if not os.path.isdir(root):
            continue
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for name in entries:
            full = os.path.join(root, name)
            if not os.path.isfile(full):
                continue
            if os.path.splitext(name)[1].lower() not in VIDEO_EXT:
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            if st.st_size < 100000:
                continue
            if full in seen:
                continue
            seen.add(full)
            out.append({"path": full, "name": name, "dir": root,
                        "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out


def serve_range(path, handler, content_type=None):
    """Serve a file, honouring Range so <video> can seek."""
    size = os.path.getsize(path)
    ctype = content_type or mimetypes.guess_type(path)[0] or "application/octet-stream"
    rng = handler.headers.get("Range")
    start, end = 0, size - 1
    status = 200
    if rng:
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        if m:
            if m.group(1):
                start = int(m.group(1))
            if m.group(2):
                end = int(m.group(2))
            if start >= size:
                handler.send_response(416)
                handler.send_header("Content-Range", f"bytes */{size}")
                handler.end_headers()
                return
            status = 206
    length = end - start + 1
    handler.send_response(status)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Content-Length", str(length))
    if status == 206:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.end_headers()
    with open(path, "rb") as fh:
        fh.seek(start)
        left = length
        while left > 0:
            chunk = fh.read(min(256 * 1024, left))
            if not chunk:
                break
            try:
                handler.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                return
            left -= len(chunk)


class Handler(BaseHTTPRequestHandler):
    server_version = "UpscalerGUI/1.0"

    def log_message(self, fmt, *args):
        pass

    # --- helpers -----------------------------------------------------------

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _json_body(self):
        try:
            # Read once: a second read would find the body already consumed.
            if getattr(self, "_body_cache", None) is None:
                self._body_cache = json.loads(
                    self._body().decode("utf-8", "replace") or "{}")
            return self._body_cache
        except Exception:
            return {}

    # --- routes ------------------------------------------------------------

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        path = u.path

        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path == "/api/models":
            import rental
            # The image the job would use decides which engines exist. The
            # GUI asks again with the image that is in the field, so the
            # button says the truth for the image that will actually run.
            want_image = (q.get("image") or [""])[0].strip()
            return self._json({
                "models": engine.available_models(),
                "encoders": [{"id": k, "label": v[0]} for k, v in engine.ENCODERS.items()],
                "binary": engine.BINARY,
                "binary_ok": os.path.exists(engine.BINARY),
                "rates": engine.RATES,
                "looks": [{"id": k, "label": v["label"]} for k, v in engine.LOOKS.items()],
                # The image each engine runs in when the field is empty.
                # They are not the same: the diffusion engines live in
                # images of their own.
                "default_images": {
                    "realesrgan": rental.DEFAULT_IMAGE,
                    "dloral": rental.DEFAULT_IMAGE_DLORAL,
                },
                # The short side the SeedVR2 diffusion is asked for when
                # there is no cap. The GUI shows the size of the result,
                # and it has to be the same number the job uses.
                "seedvr2_short": engine.SEEDVR2_DEFAULT_SHORT,
                "presets": [dict(p, **engine.preset_availability(p, want_image or None))
                             for p in engine.PRESETS],
            })
        if path == "/api/cloud/health":
            import cloud
            endpoint = (q.get("endpoint") or [""])[0].rstrip("/")
            if not endpoint:
                return self._json({"error": "no endpoint"}, 400)
            try:
                return self._json({"ok": True, "worker": cloud.health(endpoint)})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:300]}, 200)
        if path == "/api/cloud/estimate":
            import cloud
            try:
                frames = int((q.get("frames") or ["0"])[0])
                model = (q.get("model") or ["xintao-realesr-animevideo-x2-v3"])[0]
                price = (q.get("price") or ["runpod-l40s"])[0]
                motion = int((q.get("motion") or ["0"])[0])
                eng = (q.get("engine") or ["realesrgan"])[0]
                return self._json(cloud.estimate(frames, model, price, motion=motion,
                                                 engine=eng))
            except Exception as e:
                return self._json({"error": str(e)}, 400)

        if path == "/api/rent/gpus":
            import rental
            import engine as engine_mod
            engine_name = (q.get("engine") or ["realesrgan"])[0]
            # The cards depend on the size of the RESULT, not the source:
            # a DLoRAL job that ends at 720p fits on a 24 GB card and one
            # that ends at 1080p does not. So the size of the result is
            # worked out here, from the source and the cap.
            height = None
            try:
                src_w = int((q.get("w") or ["0"])[0])
                src_h = int((q.get("h") or ["0"])[0])
                cap = (q.get("cap") or [""])[0]
                if src_w and src_h:
                    target = engine_mod.target_size(src_w, src_h, cap)
                    height = target[1] if target else src_h
            except Exception:
                height = None
            return self._json({"gpus": rental.gpu_choices(
                engine_name, out_height=height)})
        if path == "/api/agents":
            return self._json(agents_state())

        if path == "/api/approvals":
            # The pending approvals, for the GUI to show: this is where an
            # agent's request for a GPU becomes a price the user can say
            # yes or no to.
            out = []
            for a in APPROVALS.values():
                if a["state"] == "pending" and a["expires"] < time.time():
                    a["state"] = "expired"
                if a["state"] in ("pending", "approved"):
                    out.append({k: a.get(k) for k in
                                ("id", "state", "path", "engine", "gpu",
                                 "cost", "cold_cost", "created", "expires")})
            return self._json({"approvals": out})

        if path == "/api/presets":
            # What this thing can do, in one answer: the engines, the
            # looks, the images and the cards with their live prices. An
            # agent reads this before it asks for anything.
            import rental as rental_mod
            engines = []
            for name in ("realesrgan", "seedvr2", "dloral", "star"):
                img = (rental_mod.DEFAULT_IMAGE if name == "realesrgan"
                       else rental_mod.DEFAULT_IMAGE_SEEDVR2 if name == "seedvr2"
                       else rental_mod.DEFAULT_IMAGE_DLORAL if name == "dloral"
                       else rental_mod.DEFAULT_IMAGE_STAR)
                engines.append({
                    "id": name,
                    "image": img,
                    "description": {
                        "realesrgan": "Real-ESRGAN on a rented card: the "
                                      "plain enlargement, the cheapest route.",
                        "seedvr2": "SeedVR2: a diffusion model, 3.4 GB of "
                                   "weights baked into the image.",
                        "dloral": "DLoRAL: a one-step diffusion built for "
                                  "temporal consistency. 11.8 GB of weights, "
                                  "and 48 GB of VRAM for a 1080p result.",
                        "star": "STAR: a steerable diffusion that "
                                "regenerates a short clip from a prompt. "
                                "48 GB of VRAM, and it is a tool rather "
                                "than a film pass.",
                    }.get(name, ""),
                    "cards": [{"id": c["id"], "price_per_hour": c.get("price_per_hour"),
                               "vram_gb": c.get("vram")} for c in
                              rental_mod.gpu_choices(name)],
                })
            return self._json({
                "engines": engines,
                "looks": [{"id": k, **{kk: v[kk] for kk in
                                      ("blend", "soften", "grain", "halation")
                                      if kk in v}} for k, v in engine.LOOKS.items()],
                "backends": [
                    {"id": "realesrgan", "description": "the local worker on "
                     "this Mac: no card, no money, a fraction of the quality"},
                    {"id": "cloud", "description": "a rented card: the "
                     "diffusion engines, and the cost that goes with them"},
                    {"id": "star", "description": "the STAR preset"},
                ],
                "openapi": "/api/openapi.json",
            })

        if path == "/api/openapi.json":
            # The API, described: what an agent (or anything else) can ask
            # this server for, and what it gets back.
            doc = os.path.join(HERE, "api-anything", "openapi.json")
            if os.path.isfile(doc):
                with open(doc, "rb") as fh:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(os.path.getsize(doc)))
                    self.end_headers()
                    self.wfile.write(fh.read())
                    return
            return self._json({"error": "no openapi document"}, 404)

        if path == "/api/rent/where":
            import keys
            provider = (q.get("provider") or ["runpod"])[0]
            return self._json({"provider": provider, "where": keys.where(provider)})
        if path == "/api/rent/check":
            import rental
            provider = (q.get("provider") or ["runpod"])[0]
            key = (q.get("key") or [""])[0]
            source = "the key you pasted"
            if not key:
                import keys
                key = keys.resolve(provider) or ""
                source = "the stored key"
            if provider == "runpod" and not key:
                return self._json({"ok": False, "error": "no API key found"})
            if provider != "runpod":
                return self._json({"ok": True, "provider": provider})
            try:
                r = rental.make(provider="runpod", api_key=key, log=lambda m: None)
                data = r._request("GET", "/pods")
                pods = data.get("data") if isinstance(data, dict) else data
                n = len(pods) if isinstance(pods, list) else 0
                return self._json({"ok": True, "provider": provider,
                                   "pods": n, "source": source})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:300]})

        if path == "/api/rent/warm":
            # The cards that are being kept between jobs: stopped, with the
            # GPU free and the weights still on their disk. They cost the
            # volume disk until they are thrown away, so there has to be a
            # way to see them and a way to clear them.
            import rental
            import keys
            provider = (q.get("provider") or ["runpod"])[0]
            try:
                key = keys.resolve(provider) or ""
                r = rental.make(provider=provider, api_key=key,
                                log=lambda m: None)
                pods = r.warm_pods()
                rows = [{"id": p.get("id"), "name": p.get("name"),
                         "image": p.get("image"),
                         "gpu": (p.get("gpu") or {}).get("id"),
                         "cost": p.get("cost"),
                         "disk": p.get("disk")} for p in pods]
                if (q.get("clear") or [""])[0] in ("1", "true", "yes"):
                    for p in pods:
                        try:
                            r._request("DELETE", f"/pods/{p.get('id')}")
                        except Exception as e:
                            return self._json({"ok": False,
                                               "error": str(e)[:300]})
                    return self._json({"ok": True, "cleared": len(pods),
                                       "pods": []})
                return self._json({"ok": True, "pods": rows})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:300]})

        if path == "/api/star":
            import star
            ok, why = star.available()
            worth, lines = star.preflight()
            return self._json({
                "available": ok, "reason": why,
                "worth_trying": worth,
                "spaces": star.space_status(),
                "note": star.DEMO_NOTE,
                "defaults": {
                    "width": star.DEFAULT_WIDTH,
                    "fps": star.DEFAULT_FPS,
                    "seconds": star.DEFAULT_SECONDS,
                    "prompt": star.DEFAULT_PROMPT,
                },
            })
        if path == "/api/library":
            return self._json({"files": scan_library()})
        if path == "/api/job":
            job = JOBS.get((q.get("id") or [""])[0])
            if not job:
                return self._json({"error": "no such job"}, 404)
            return self._json({
                "id": job.id, "state": job.state, "stage": job.stage,
                "progress": round(job.progress, 4), "message": job.message,
                "error": job.error, "log": job.log[-120:],
                "frames_done": job.frames_done, "frames_total": job.frames_total,
                "eta": job.eta, "output": job.out if job.state == "done" else None,
                "elapsed": time.time() - job.started,
            })
        if path == "/api/source":
            p = (q.get("path") or [""])[0]
            if not p or not os.path.isfile(p) or not allowed_path(p):
                return self._json({"error": "not allowed"}, 403)
            return serve_range(p, self)
        if path == "/api/download":
            job = JOBS.get((q.get("id") or [""])[0])
            if not job or job.state != "done" or not os.path.exists(job.out):
                return self._json({"error": "not ready"}, 404)
            return serve_range(job.out, self, "application/octet-stream")
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        path = u.path

        if path == "/api/probe":
            body = self._json_body()
            p = body.get("path") or ""
            if not p or not os.path.isfile(p) or not allowed_path(p):
                return self._json({"error": "not allowed"}, 403)
            try:
                return self._json(engine.video_info(p))
            except Exception as e:
                return self._json({"error": str(e)}, 400)

        if path == "/api/upload":
            name = (q.get("name") or ["upload.mp4"])[0]
            name = os.path.basename(name).replace("/", "_")
            data = self._body()
            dest = os.path.join(UPLOADS, name)
            with open(dest, "wb") as fh:
                fh.write(data)
            try:
                info = engine.video_info(dest)
            except Exception as e:
                os.remove(dest)
                return self._json({"error": str(e)}, 400)
            return self._json({"path": dest, "info": info})

        if path == "/api/estimate":
            # What would this job cost if it rented a GPU? No side effects.
            try:
                body = self._json_body()
                src = body.get("path") or ""
                if not src or not os.path.exists(src):
                    return self._json({"error": "pick a clip first"}, 400)
                est = engine.estimate_rental(body, src)
                # A rental estimate comes with a pending approval: the
                # price the user has to agree to before anything is rented.
                if body.get("rent") and not est.get("error"):
                    est["approval_id"] = new_approval(
                        est, src, body.get("cloud_engine") or "")
                return self._json(est)
            except Exception as e:
                return self._json({"error": str(e)}, 400)

        if path == "/api/run":
            # A job that would rent a GPU needs the approval that came with
            # the estimate. Refuse it here, before anything is created, so
            # the answer is immediate and nothing can be rented by accident.
            try:
                _b = self._json_body()
                if _b.get("rent"):
                    # The consent has to be the user's: an approval the
                    # server issued and the user agreed to. An agent cannot
                    # write rent_approved itself.
                    ok, why = approval_matches(
                        APPROVALS.get(_b.get("rent_approval") or ""), _b)
                    if not ok:
                        return self._json({
                            "error": "this job would rent a GPU and it has "
                                     "not been approved: " + why + ". Ask for "
                                     "the estimate first, and let the user "
                                     "approve it.",
                            "needs_approval": True,
                            "estimate": engine.estimate_rental(
                                _b, _b.get("path") or ""),
                        }, 400)
                    # The approval IS the consent: the engine's own gate
                    # sees it and lets the pod be created.
                    _b["rent_approved"] = True
                    _b["rent_estimate"] = APPROVALS[
                        _b["rent_approval"]]["estimate"]
            except Exception:
                pass
            body = self._json_body()
            src = body.get("path") or ""
            if not src or not os.path.isfile(src) or not allowed_path(src):
                return self._json({"error": "not allowed"}, 403)
            backend = body.get("backend") or "realesrgan"
            if backend not in ("realesrgan", "star", "cloud"):
                return self._json({"error": "unknown backend"}, 400)
            cloud_engine = body.get("cloud_engine") or "realesrgan"
            model = body.get("model") or "xintao-realesr-animevideo-x2-v3"
            # 0 means "the engine decides": DLoRAL is a 4x model and the
            # others are 2x, and the server used to flatten that to 2 for
            # every caller that did not name a scale - so an agent (or any
            # API caller) got a DLoRAL job that was half the size it
            # asked for.
            scale = int(body.get("scale") or 0)
            # The diffusion engines have a scale of their own and do not use
            # a Real-ESRGAN model at all. Checking the scale against the
            # default model made the DLoRAL preset impossible to start: it
            # asks for 4x, the default model only does 2x, and the job was
            # refused before it began. SeedVR2 was missed here, and the
            # same refusal stopped that engine from ever running.
            if not (backend == "cloud" and cloud_engine in engine.DIFFUSION_ENGINES):
                if model not in engine.MODELS:
                    return self._json({"error": "unknown model"}, 400)
                max_scale = engine.MODELS[model][1]
                if max_scale and scale > max_scale:
                    return self._json({"error": f"{model} cannot go beyond {max_scale}x"}, 400)
            encoder = body.get("encoder") or "h264"
            if encoder not in engine.ENCODERS:
                return self._json({"error": "unknown encoder"}, 400)

            job_id = uuid.uuid4().hex[:12]
            stem = os.path.splitext(os.path.basename(src))[0]
            ext = ".mp4"
            if encoder == "vp9":
                ext = ".webm"
            elif encoder == "prores":
                ext = ".mov"
            if backend == "star":
                tag = "star"
                ext = ".mp4"
            else:
                tag = f"x{scale}"
            out_path = os.path.join(OUTBOX, f"{stem}-{tag}-{job_id}{ext}")
            job_work = os.path.join(JOBS_DIR, job_id)
            # Before the job's folder exists: if the disk is nearly full,
            # the scratch space goes onto the iCloud Drive, where macOS
            # will reclaim it as the disk fills up. Moving it while another
            # job is running would move its files out from under it.
            try:
                import tools.space_guard as space_guard
                note = space_guard.ensure_space(log=lambda m: print(m, flush=True))
                if note:
                    print("space guard: " + note, flush=True)
            except Exception as e:
                print(f"space guard: could not check the disk ({e})", flush=True)
            os.makedirs(job_work, exist_ok=True)

            job = engine.Job(job_id, src, out_path, {
                "backend": backend,
                "model": model, "scale": scale, "encoder": encoder,
                "look": body.get("look") or "clean",
                "denoise_engine": body.get("denoise_engine") or "ffmpeg",
                "bilateral_radius": body.get("bilateral_radius") or 7,
                "bilateral_strength": body.get("bilateral_strength") or 0.85,
                "blend_override": body.get("blend_override"),
                "max_resolution": body.get("max_resolution") or "",
                "deinterlace": body.get("deinterlace") or "auto",
                "crop": body.get("crop") or "",
                "motion": int(body.get("motion") or 0),
                "chunk_frames": body.get("chunk_frames") or 30,
                # STAR options
                "star_prompt": body.get("star_prompt") or "",
                "star_start": body.get("star_start") or 0,
                "star_seconds": body.get("star_seconds") or 2,
                "star_width": body.get("star_width") or 320,
                "star_upscale": body.get("star_upscale") or 4,
                "star_chunk_len": body.get("star_chunk_len") or 24,
                "star_chunk_size": body.get("star_chunk_size") or 3,
                "hf_token": body.get("hf_token") or "",
                # cloud GPU worker
                "cloud_endpoint": body.get("cloud_endpoint") or "",
                "cloud_key": body.get("cloud_key") or "",
                "cloud_engine": body.get("cloud_engine") or "realesrgan",
                # The direction, for the engines that can be steered.
                "prompt": body.get("prompt") or "",
                # renting
                "rent": bool(body.get("rent")),
                # The approval and the estimate the user saw have to travel
                # with the job: without them the job refuses to rent, and
                # the refusal is the whole point of the gate.
                "rent_approved": bool(body.get("rent_approved")),
                "rent_keep_warm": bool(body.get("rent_keep_warm")),
                "rent_estimate": body.get("rent_estimate") or {},
                "rent_provider": body.get("rent_provider") or "runpod",
                "rent_key": body.get("rent_key") or "",
                "rent_gpu": body.get("rent_gpu") or "",
                "rent_image": body.get("rent_image") or "",
                "rent_region": body.get("rent_region") or "",
            }, job_work)
            JOBS[job_id] = job
            job.start()
            return self._json({"id": job_id})

        if path == "/api/fetch":
            # A video from a URL, for the agents and for the GUI's source
            # box: a direct link is downloaded, and a site with no API is
            # reached through API Anything.
            try:
                body = self._json_body()
                dest = fetch_video((body.get("url") or "").strip(),
                                   log=lambda m: print(m, flush=True))
                return self._json({"path": dest,
                                    "info": engine.video_info(dest)})
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 400)

        if path == "/api/agents/install":
            if AGENTS.get("busy"):
                return self._json({"ok": True, "busy": True,
                                   "log": AGENTS.get("log", "")})
            t = threading.Thread(target=agents_install)
            t.daemon = True
            t.start()
            return self._json({"ok": True, "busy": True, "log": "connecting..."})

        if path == "/api/approve":
            # The user says yes (or no) to a price. This is the only way
            # a rental ever starts.
            try:
                body = self._json_body()
                a = APPROVALS.get(body.get("id") or "")
                if not a:
                    return self._json({"error": "there is no such approval"},
                                       404)
                a["state"] = "approved" if body.get("yes") else "declined"
                return self._json({"ok": True, "id": a["id"],
                                    "state": a["state"]})
            except Exception as e:
                return self._json({"error": str(e)}, 400)

        if path == "/api/cancel":
            body = self._json_body()
            job = JOBS.get(body.get("id") or "")
            if job:
                job.cancel()
            return self._json({"ok": True})

        return self._json({"error": "not found"}, 404)

    def _static(self, name):
        full = os.path.join(HERE, name)
        if not os.path.isfile(full):
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(full)[0] or "text/plain"
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    args = ap.parse_args()
    ensure_dirs()

    # A pod from an earlier run whose job died with the process is still
    # billing, and nothing else will ever look at it. The ids of the pods
    # this machine creates are written down, so the next start can finish
    # what the last one did not.
    try:
        import rental as rental_mod
        import keys as keys_mod
        api_key = keys_mod.resolve("runpod")
        if api_key:
            r = rental_mod.RunPod(api_key, log=lambda m: print(m, flush=True))
            r.sweep_orphans()
    except Exception as e:
        print("could not check for orphaned pods: %s" % str(e)[:120], flush=True)

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Upscaler GUI on http://127.0.0.1:{args.port}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
