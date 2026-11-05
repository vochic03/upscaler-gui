#!/usr/bin/env python3
"""
Drive the real Chrome via CDP: trusted clicks, typing, and reading results.

    ./cdp_drive.py click <url-substring> <selector>
    ./cdp_drive.py type  <url-substring> <selector> <text>
    ./cdp_drive.py eval  <url-substring> <js>
    ./cdp_drive.py text  <url-substring> <selector>
    ./cdp_drive.py blob  <url-substring> <selector> <outfile>
"""

import asyncio
import base64
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


async def send(ws, method, params=None, msg_id=1, timeout=30):
    await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        resp = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if resp.get("id") == msg_id:
            return resp


async def evaluate(ws, expr, await_promise=True):
    r = await send(ws, "Runtime.evaluate", {
        "expression": expr, "awaitPromise": await_promise, "returnByValue": True})
    res = r.get("result", {})
    if "exceptionDetails" in res:
        return {"__error__": res["exceptionDetails"].get("text")}
    return res.get("result", {}).get("value")


async def rect_of(ws, selector):
    js = (f"(() => {{const e=document.querySelector({json.dumps(selector)});"
          "if(!e) return null; const r=e.getBoundingClientRect();"
          "return {x:r.x+r.width/2, y:r.y+r.height/2};})()")
    return await evaluate(ws, js)


async def trusted_click(ws, selector):
    rect = await rect_of(ws, selector)
    if not rect:
        return {"__error__": f"selector not found: {selector}"}
    x, y = rect["x"], rect["y"]
    for etype in ("mousePressed", "mouseReleased"):
        await send(ws, "Input.dispatchMouseEvent", {
            "type": etype, "x": x, "y": y, "button": "left",
            "clickCount": 1, "buttons": 1 if etype == "mousePressed" else 0})
    return {"clicked": selector, "at": [x, y]}


async def run(sub, action, arg1=None, arg2=None):
    t = find_target(sub)
    if not t:
        print(json.dumps({"__error__": f"no tab matching {sub!r}"}))
        return
    async with websockets.connect(t["webSocketDebuggerUrl"], max_size=None, origin=None,
                                 ping_interval=None) as ws:
        if action == "front":
            await send(ws, "Page.bringToFront", {})
            r = {"front": True}
        elif action == "click":
            r = await trusted_click(ws, arg1)
        elif action == "type":
            await evaluate(ws, f"(() => {{const e=document.querySelector({json.dumps(arg1)});"
                              f"e.value={json.dumps(arg2)};"
                              "e.dispatchEvent(new Event('input',{bubbles:true}));return e.value;})()")
            r = {"typed": arg1}
        elif action == "eval":
            r = await evaluate(ws, arg1)
        elif action == "text":
            r = await evaluate(ws, f"(document.querySelector({json.dumps(arg1)})||{{}}).textContent")
        elif action == "blob":
            # fetch the blob behind an <a download> and write it out
            js = (f"(async () => {{const a=document.querySelector({json.dumps(arg1)});"
                  "if(!a) return null; const b=await (await fetch(a.href)).blob();"
                  "const buf=new Uint8Array(await b.arrayBuffer()); let s='';"
                  "const CH=0x8000; for(let i=0;i<buf.length;i+=CH)"
                  "s+=String.fromCharCode.apply(null,buf.subarray(i,i+CH));"
                  "return btoa(s);})()")
            data = await evaluate(ws, js)
            if not data or isinstance(data, dict):
                print(json.dumps({"__error__": "no blob", "got": data}))
                return
            with open(arg2, "wb") as fh:
                fh.write(base64.b64decode(data))
            r = {"saved": arg2, "bytes": len(base64.b64decode(data))}
        else:
            r = {"__error__": f"unknown action {action}"}
        print(json.dumps(r, indent=2, ensure_ascii=False)[:4000])


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    asyncio.run(run(sys.argv[1], sys.argv[2],
                    sys.argv[3] if len(sys.argv) > 3 else None,
                    sys.argv[4] if len(sys.argv) > 4 else None))
