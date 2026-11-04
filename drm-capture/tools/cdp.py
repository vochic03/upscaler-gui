#!/usr/bin/env python3
"""
Minimal Chrome DevTools Protocol client for driving the real Google Chrome.

    ./cdp.py list                          # list open tabs
    ./cdp.py eval <url-substring> '<js>'   # run JS in the matching tab
    ./cdp.py open <url>                    # open a new tab
    ./cdp.py shot <url-substring> <file>   # screenshot a tab
    ./cdp.py cookies <url-substring> <url> # cookies the browser would send

Chrome must be started with --remote-debugging-port=9222.
"""

import asyncio
import base64
import json
import sys
import urllib.request

import websockets

PORT = 9222


def http_json(path, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def list_targets():
    return http_json("/json/list")


def find_target(sub):
    sub = sub.lower()
    for t in list_targets():
        if t.get("type") == "page" and (t.get("id", "").lower().startswith(sub) or sub in t.get("url", "").lower()):
            return t
    return None


async def _send(ws, method, params=None, msg_id=1, timeout=float(__import__('os').environ.get('CDP_TIMEOUT', '30'))):
    await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout)
        resp = json.loads(raw)
        if resp.get("id") == msg_id:
            return resp


async def _eval(ws_url, expression, await_promise=True):
    async with websockets.connect(ws_url, max_size=None, origin=None,
                                 open_timeout=15, ping_interval=None) as ws:
        resp = await _send(ws, "Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": True,
        })
        r = resp.get("result", {})
        if "exceptionDetails" in r:
            det = r["exceptionDetails"]
            return {"__error__": det.get("text"),
                    "detail": det.get("exception", {}).get("description")}
        return r.get("result", {}).get("value")


def cmd_eval(sub, expr):
    t = find_target(sub)
    if not t:
        print(json.dumps({"__error__": f"no tab matching {sub!r}"}))
        return 1
    val = asyncio.run(_eval(t["webSocketDebuggerUrl"], expr))
    print(json.dumps(val, indent=2, ensure_ascii=False))
    return 0


def cmd_open(url):
    # Chrome 111+ requires PUT for /json/new.
    t = http_json(f"/json/new?{url}", method="PUT")
    print(json.dumps({"opened": t.get("url"), "id": t.get("id")}, indent=2))
    return 0


def cmd_cookies(sub, url):
    t = find_target(sub)
    if not t:
        print(json.dumps({"__error__": f"no tab matching {sub!r}"}))
        return 1

    async def run():
        async with websockets.connect(t["webSocketDebuggerUrl"], max_size=None,
                                     origin=None, ping_interval=None) as ws:
            return await _send(ws, "Network.getCookies", {"urls": [url]})

    resp = asyncio.run(run())
    res = resp.get("result", {})
    out = []
    for c in res.get("cookies", []):
        out.append({"name": c.get("name"), "value": c.get("value"),
                    "domain": c.get("domain"), "path": c.get("path"),
                    "secure": c.get("secure"), "httpOnly": c.get("httpOnly"),
                    "expires": c.get("expires")})
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_shot(sub, path):
    t = find_target(sub)
    if not t:
        print(json.dumps({"__error__": f"no tab matching {sub!r}"}))
        return 1

    async def run():
        async with websockets.connect(t["webSocketDebuggerUrl"], max_size=None,
                                     origin=None, ping_interval=None) as ws:
            r = await _send(ws, "Page.captureScreenshot", {"format": "png"})
            data = r.get("result", {}).get("data")
            if data:
                with open(path, "wb") as fh:
                    fh.write(base64.b64decode(data))
            return bool(data)

    ok = asyncio.run(run())
    print(json.dumps({"screenshot": path if ok else None}))
    return 0 if ok else 1


def cmd_list():
    rows = []
    for t in list_targets():
        if t.get("type") != "page":
            continue
        rows.append({"title": t.get("title"), "url": t.get("url"), "id": t.get("id")})
    print(json.dumps(rows, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "list":
        sys.exit(cmd_list())
    if cmd == "eval" and len(sys.argv) >= 4:
        sys.exit(cmd_eval(sys.argv[2], sys.argv[3]))
    if cmd == "open" and len(sys.argv) >= 3:
        sys.exit(cmd_open(sys.argv[2]))
    if cmd == "cookies" and len(sys.argv) >= 4:
        sys.exit(cmd_cookies(sys.argv[2], sys.argv[3]))
    if cmd == "shot" and len(sys.argv) >= 4:
        sys.exit(cmd_shot(sys.argv[2], sys.argv[3]))
    print(__doc__)
    sys.exit(2)
