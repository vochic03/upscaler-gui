#!/usr/bin/env python3
"""
Compare what the browser produced against ffmpeg's own decryption.

The browser run (test/selftest.html) writes the decrypted init segment and
media segments into ./out/. This script reassembles them per track, asks
ffmpeg to extract the elementary streams, and compares them byte-for-byte
with the ground truth produced by test/make_fixture.py.

    python3 test/verify.py            # check every fixture
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "out")
GT = os.path.join(HERE, "fixtures", "gt")
TMP = os.path.join(HERE, "_verify")

KEY = "000102030405060708090a0b0c0d0e0f"

FIXTURES = [
    # tag,           manifest kind, ground truth prefix
    ("dash-ctr", "dash", "dash-ctr"),
    ("dash-cbc", "dash", "dash-cbc"),
    ("hls-ctr", "hls", "hls-ctr"),
    ("hls-cbc", "hls", "hls-cbc"),
]


def run(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT).returncode == 0


def rebuild_track(tag, kind):
    """Concatenate the browser's dumped init + segments for one track."""
    init = os.path.join(OUT, f"{tag}-init-{kind}.bin")
    if not os.path.exists(init):
        return None
    segs = sorted(f for f in os.listdir(OUT) if f.startswith(f"{tag}-seg-{kind}-"))
    if not segs:
        return None
    path = os.path.join(TMP, f"{tag}-{kind}.mp4")
    with open(path, "wb") as fh:
        fh.write(open(init, "rb").read())
        for s in segs:
            fh.write(open(os.path.join(OUT, s), "rb").read())
    return path


def extract(path, stream, out):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
           "-map", f"0:{stream}", "-c", "copy", "-f", "h264" if stream == "v" else "adts", out]
    return run(cmd)


def compare(a, b):
    if not os.path.exists(a) or not os.path.exists(b):
        return False, "missing file"
    sa, sb = os.path.getsize(a), os.path.getsize(b)
    if sa != sb:
        return False, f"size {sa} != {sb}"
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            x, y = fa.read(65536), fb.read(65536)
            if x != y:
                return False, "content differs"
            if not x:
                break
    return True, f"identical ({sa} bytes)"


def main():
    os.makedirs(TMP, exist_ok=True)
    failures = 0
    checked = 0

    for tag, _kind, gtprefix in FIXTURES:
        for track, stream, ext in (("video", "v", "h264"), ("audio", "a", "aac")):
            path = rebuild_track(tag, track)
            if path is None:
                print(f"  SKIP  {tag}/{track}: no browser output")
                continue
            mine = os.path.join(TMP, f"{tag}-{track}.{ext}")
            if not extract(path, stream, mine):
                print(f"  FAIL  {tag}/{track}: ffmpeg could not read the browser output")
                failures += 1
                continue
            gt = os.path.join(GT, f"{gtprefix}-{track}.{ext}")
            ok, why = compare(mine, gt)
            checked += 1
            if ok:
                print(f"  PASS  {tag}/{track}: {why}")
            else:
                print(f"  FAIL  {tag}/{track}: {why}")
                failures += 1

    print(f"\n{checked - failures}/{checked} comparisons passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
