#!/usr/bin/env python3
"""
Fill the app's fields from a preset, so nothing has to be pasted by hand.

    ./tools/preset.py mne            # fill the MNE Play preset
    ./tools/preset.py mne --load     # ...and press Load straight away
    ./tools/preset.py mne --delay 7200
    ./tools/preset.py mne --delay 20:15      # a clock time instead of seconds

The preset file (presets/<name>.txt) is the cURL command; presets/<name>.key
holds "<KID>\\n<CEK>". Both are read from disk, so they can be edited without
touching this script.

The app tab is found by its URL, so any tab showing the app works.
"""

import argparse
import asyncio
import json
import os
import sys
import urllib.request

import websockets

PORT = 9222
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP_URL = "127.0.0.1:8765"


def http_json(path, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def find_app_tab():
    for t in http_json("/json/list"):
        if t.get("type") == "page" and APP_URL in t.get("url", ""):
            return t
    return None


def read_preset(name):
    txt_path = os.path.join(ROOT, "presets", name + ".txt")
    key_path = os.path.join(ROOT, "presets", name + ".key")
    with open(txt_path) as f:
        command = f.read().strip()
    kid = cek = ""
    if os.path.exists(key_path):
        with open(key_path) as f:
            lines = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        if lines:
            kid = lines[0]
        if len(lines) > 1:
            cek = lines[1]
    return command, kid, cek


async def run_eval(tab, expression):
    ws_url = tab["webSocketDebuggerUrl"]
    async with websockets.connect(ws_url, max_size=None, origin=None,
                                 open_timeout=15, ping_interval=None) as ws:
        await ws.send(json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": expression, "awaitPromise": True,
                       "returnByValue": True},
        }))
        while True:
            resp = json.loads(await asyncio.wait_for(ws.recv(), 30))
            if resp.get("id") == 1:
                if "exceptionDetails" in resp.get("result", {}):
                    raise SystemExit("eval failed: " + json.dumps(resp["result"])[:400])
                return resp.get("result", {}).get("result", {}).get("value")


def js_string(s):
    return json.dumps(s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name", nargs="?", default="mne-play", help="preset name (presets/<name>.txt)")
    ap.add_argument("--delay", default=None,
                    help="catch-up delay: seconds, or HH:MM, or YYYY-MM-DD HH:MM")
    ap.add_argument("--load", action="store_true", help="press Load when done")
    ap.add_argument("--play", action="store_true", help="press Play when done")
    args = ap.parse_args()

    tab = find_app_tab()
    if not tab:
        raise SystemExit("no app tab found - open http://127.0.0.1:8765/ in Chrome first")

    try:
        command, kid, cek = read_preset(args.name)
    except FileNotFoundError:
        raise SystemExit(f"no preset {args.name!r} in {os.path.join(ROOT, 'presets')}")

    delay = args.delay

    expr = f"""
(function() {{
  var cmd = {js_string(command)};
  var d = document.getElementById("delay");
  if ({js_string(delay)} !== null) {{ d.value = {js_string(delay)}; }}
  document.getElementById("command").value = cmd;
  document.getElementById("parse").click();
  var k = document.getElementById("key"); k.value = {js_string(cek)};
  k.dispatchEvent(new Event("input"));
  var ki = document.getElementById("kid"); ki.value = {js_string(kid)};
  ki.dispatchEvent(new Event("input"));
  d.dispatchEvent(new Event("input"));
  return JSON.stringify({{
    url: document.getElementById("manifestUrl").value.slice(0, 90),
    headers: document.getElementById("headers").value.split("\\n").length,
    key: k.value.length, kid: ki.value.length,
    delayInfo: document.getElementById("delayInfo").textContent
  }});
}})()
"""
    out = asyncio.run(run_eval(tab, expr))
    print("filled:", out)

    if args.load:
        print("loading...")
        asyncio.run(run_eval(tab, 'document.getElementById("load").click(); "clicked"'))
        if args.play:
            asyncio.run(asyncio.sleep(6))
            asyncio.run(run_eval(tab, 'document.getElementById("play").click(); "clicked"'))
        print("done - watch the log panel")


if __name__ == "__main__":
    main()
