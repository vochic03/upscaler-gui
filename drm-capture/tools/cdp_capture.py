#!/usr/bin/env python3
"""
Capture a tab's network traffic over CDP.

    ./cdp_capture.py <url-substring> <seconds> <out.json>

Attaches to the tab, enables the Network domain, reloads the page and records
every request/response (headers + bodies) so the licence exchange can be
inspected.
"""

import asyncio
import json
import sys
import urllib.request

import websockets

PORT = 9222


def find_target(sub):
    sub = sub.lower()
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=10) as r:
        for t in json.load(r):
            if t.get("type") == "page" and (t.get("id", "").lower().startswith(sub) or sub in t.get("url", "").lower()):
                return t
    return None


async def capture(ws_url, seconds, out_path):
    captured = {"requests": {}, "responses": {}, "bodies": {}, "console": []}
    async with websockets.connect(ws_url, max_size=None, origin=None,
                                 open_timeout=15, ping_interval=None) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Network.enable",
                                  "params": {"maxPostDataSize": 1 << 20,
                                             "maxResourceBufferSize": 1 << 24,
                                             "maxTotalBufferSize": 1 << 25}}))
        await ws.send(json.dumps({"id": 2, "method": "Page.enable"}))
        await ws.send(json.dumps({"id": 3, "method": "Page.reload",
                                  "params": {"ignoreCache": False}}))
        deadline = asyncio.get_event_loop().time() + seconds
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), max(0.2, deadline - asyncio.get_event_loop().time()))
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            method = msg.get("method", "")
            p = msg.get("params", {})
            if method == "Network.requestWillBeSent":
                rid = p.get("requestId")
                captured["requests"][rid] = {
                    "url": p.get("request", {}).get("url"),
                    "method": p.get("request", {}).get("method"),
                    "headers": p.get("request", {}).get("headers"),
                    "postData": (p.get("request", {}).get("postData") or "")[:4000],
                }
            elif method == "Network.responseReceived":
                rid = p.get("requestId")
                resp = p.get("response", {})
                captured["responses"][rid] = {
                    "url": resp.get("url"),
                    "status": resp.get("status"),
                    "mimeType": resp.get("mimeType"),
                    "headers": resp.get("headers"),
                }
            elif method == "Network.loadingFinished":
                rid = p.get("requestId")
                try:
                    await ws.send(json.dumps({"id": 1000 + len(captured["bodies"]),
                                              "method": "Network.getResponseBody",
                                              "params": {"requestId": rid}}))
                except Exception:
                    pass
            elif method == "Runtime.consoleAPICalled":
                captured["console"].append({
                    "type": p.get("type"),
                    "args": [a.get("value") or a.get("description") for a in p.get("args", [])],
                })
            elif method == "Runtime.exceptionThrown":
                captured["console"].append({"type": "exception",
                                            "args": [str(p.get("exceptionDetails", {}).get("text"))]})
            elif str(msg.get("id", "")).startswith("1000"):
                rid = str(msg.get("id"))[4:]
                body = msg.get("result", {}).get("body", "")
                if body:
                    captured["bodies"][rid] = body
    with open(out_path, "w") as fh:
        json.dump(captured, fh, indent=1)
    return captured


if __name__ == "__main__":
    sub = sys.argv[1]
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 15
    out = sys.argv[3] if len(sys.argv) > 3 else "/tmp/cdp-capture.json"
    t = find_target(sub)
    if not t:
        print("no tab matching", sub)
        sys.exit(1)
    res = asyncio.run(capture(t["webSocketDebuggerUrl"], secs, out))
    interesting = []
    for rid, r in res["requests"].items():
        u = r["url"] or ""
        if any(k in u.lower() for k in (".mpd", ".m3u8", "license", "widevine", "drm", "manifest", "key")):
            interesting.append((rid, u[:150]))
    print(f"captured {len(res['requests'])} requests, {len(res['bodies'])} bodies -> {out}")
    for rid, u in interesting:
        print(f"  [{rid}] {u}")
