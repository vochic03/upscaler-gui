#!/usr/bin/env python3
"""
Attach to a tab, run a snippet, and print every console message / exception
/ Log entry Chrome produced while it ran.

    ./cdp_console.py <tab-id-or-url> '<js expression>'
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


async def main(sub, expr):
    t = find_target(sub)
    if not t:
        print("no tab matching", sub)
        return
    msgs = []
    async with websockets.connect(t["webSocketDebuggerUrl"], max_size=None, origin=None,
                                 ping_interval=None) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        await ws.send(json.dumps({"id": 2, "method": "Log.enable"}))
        await ws.send(json.dumps({"id": 3, "method": "Media.enable"}))
        await ws.send(json.dumps({"id": 4, "method": "Runtime.evaluate",
                                  "params": {"expression": expr, "awaitPromise": True,
                                             "returnByValue": True}}))
        deadline = asyncio.get_event_loop().time() + 8
        result = None
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), max(0.2, deadline - asyncio.get_event_loop().time()))
            except asyncio.TimeoutError:
                break
            m = json.loads(raw)
            method = m.get("method")
            p = m.get("params", {})
            if method == "Runtime.consoleAPICalled":
                args = [a.get("value") if "value" in a else a.get("description") for a in p.get("args", [])]
                msgs.append(("console." + p.get("type", ""), " ".join(str(a) for a in args)))
            elif method == "Runtime.exceptionThrown":
                d = p.get("exceptionDetails", {})
                msgs.append(("exception", d.get("text", "") + " " +
                             str(d.get("exception", {}).get("description", ""))[:200]))
            elif method == "Log.entryAdded":
                e = p.get("entry", {})
                msgs.append(("log." + e.get("level", ""), f"{e.get('source','')}: {e.get('text','')}"))
            elif method == "Media.playerErrorsReceived":
                for err in p.get("errors", []):
                    msgs.append(("mediaError", json.dumps(err)[:300]))
            elif m.get("id") == 4:
                result = p
    print("=== eval result ===")
    print(json.dumps(result, indent=1)[:1500])
    print("=== browser messages ===")
    for kind, text in msgs:
        print(f"[{kind}] {text[:400]}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "1"))
