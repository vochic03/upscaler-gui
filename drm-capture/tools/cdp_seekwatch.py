#!/usr/bin/env python3
"""
Watch what a tab requests when the page seeks in its video.

    ./cdp_seekwatch.py <tab-id-or-url> <seconds-to-seek-back> [out.json]

Attaches to the tab, enables the Network domain, nudges the <video>
backwards and records every manifest request that follows.
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
            if t.get("type") == "page" and (t.get("id", "").lower().startswith(sub)
                                            or sub in t.get("url", "").lower()):
                return t
    return None


async def main(sub, back):
    t = find_target(sub)
    if not t:
        print("no tab matching", sub)
        return
    seen = []
    async with websockets.connect(t["webSocketDebuggerUrl"], max_size=None, origin=None,
                                 ping_interval=None) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
        await ws.send(json.dumps({"id": 2, "method": "Runtime.enable"}))
        await ws.send(json.dumps({"id": 3, "method": "Runtime.evaluate", "params": {
            "expression": "(() => { const v=document.querySelector('video');"
                          f"const before=v.currentTime; v.currentTime=Math.max(0, before-{back});"
                          "return JSON.stringify({before, after:v.currentTime, dur:v.duration}); })()",
            "returnByValue": True}}))
        deadline = asyncio.get_event_loop().time() + 20
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), max(0.2, deadline - asyncio.get_event_loop().time()))
            except asyncio.TimeoutError:
                break
            m = json.loads(raw)
            if m.get("method") == "Network.requestWillBeSent":
                u = m["params"]["request"]["url"]
                seen.append((m["params"]["request"]["method"], u))
    print(f"manifest requests seen: {len(seen)}")
    for u in seen:
        print("  ", u)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 600))
