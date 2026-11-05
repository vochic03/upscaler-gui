#!/usr/bin/env python3
"""
Run the whole decryption validation in one go.

    python3 test/run_selftest.py

It clears the previous dumps for each fixture, opens test/selftest.html once
per fixture in a real Chrome tab, and then compares the browser's output with
ffmpeg's own decryption.

Chrome has to be running with --remote-debugging-port=9222 and the proxy has
to be serving the app on 127.0.0.1:8765.

The cleanup matters: verify.py globs the dumped segments, so leftovers from an
older run would be concatenated with the new ones and the comparison would
fail for the wrong reason.
"""

import asyncio
import glob
import json
import os
import subprocess
import sys
import urllib.request

import websockets

PORT = 9222
ORIGIN = "http://127.0.0.1:8765"
KEY = "000102030405060708090a0b0c0d0e0f"
FIXTURES = ["dash-ctr", "dash-cbc", "hls-ctr", "hls-cbc"]

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "out")


def http_json(path, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def find_or_open_tab():
    for t in http_json("/json/list"):
        if t.get("type") == "page" and "selftest.html" in t.get("url", ""):
            return t
    t = http_json(f"/json/new?{ORIGIN}/test/selftest.html", method="PUT")
    return t


def clean(tag):
    """Remove the dumps of an earlier run for this fixture."""
    removed = 0
    for pattern in (f"{tag}-init-*.bin", f"{tag}-seg-*.mp4"):
        for path in glob.glob(os.path.join(OUT, pattern)):
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed


async def send(ws, mid, method, params=None):
    await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        resp = json.loads(await asyncio.wait_for(ws.recv(), 30))
        if resp.get("id") == mid:
            return resp


async def evaluate(ws, mid, expression):
    r = await send(ws, mid, "Runtime.evaluate",
                   {"expression": expression, "returnByValue": True})
    return r.get("result", {}).get("result", {}).get("value")


async def run_browser(tab):
    async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=None,
                                 origin=None, open_timeout=15,
                                 ping_interval=None) as ws:
        # A hidden tab never opens a MediaSource, so the page has to be in
        # the foreground.
        await send(ws, 1, "Page.bringToFront", {})
        failures = []
        for tag in FIXTURES:
            url = (f"{ORIGIN}/test/selftest.html?manifest="
                   f"{ORIGIN}/test/fixtures/{tag}/manifest.mpd&key={KEY}&tag={tag}")
            # Bring it forward again for every fixture: a tab that is not in
            # the foreground does not open a MediaSource, and Chrome can take
            # the focus away between two runs.
            await send(ws, 1, "Page.bringToFront", {})
            await send(ws, 2, "Page.navigate", {"url": url})
            title = ""
            for attempt in range(60):
                await asyncio.sleep(1)
                title = await evaluate(ws, 3, "document.title")
                if title in ("DONE", "ERROR"):
                    break
                # The MediaSource only opens in a foreground tab; if the page
                # is waiting for that, push the tab forward again.
                if attempt in (5, 15, 30):
                    await send(ws, 9, "Page.bringToFront", {})
            if title == "DONE":
                print(f"  {tag}: browser run ok")
            else:
                tail = await evaluate(ws, 4,
                                      "document.getElementById('log').textContent.slice(-400)")
                print(f"  {tag}: FAILED\n{tail}")
                failures.append(tag)
        return failures


def main():
    if not os.path.isdir(OUT):
        os.makedirs(OUT, exist_ok=True)
    try:
        tab = find_or_open_tab()
    except Exception as e:
        raise SystemExit(f"cannot reach Chrome on port {PORT}: {e}")

    print("clearing old dumps")
    for tag in FIXTURES:
        n = clean(tag)
        if n:
            print(f"  {tag}: removed {n} stale file(s)")

    print("running the browser")
    failures = asyncio.run(run_browser(tab))
    if failures:
        print(f"browser run failed for: {', '.join(failures)}")
        return 1

    print("comparing against ffmpeg")
    rc = subprocess.call([sys.executable, os.path.join(HERE, "verify.py")])
    return rc


if __name__ == "__main__":
    sys.exit(main())
