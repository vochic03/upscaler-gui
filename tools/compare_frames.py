#!/usr/bin/env python3
"""Put three frames side by side: the source, what the card made, the result.

    python3 tools/compare_frames.py <source.mp4> <ai.mp4> <final.mp4> <out.png>

It is the quickest way to tell whether a diffusion really ran: if the
middle picture is the same size as the source, nothing was regenerated.
"""
import os
import subprocess
import sys


def frame(path, out, at=0.5):
    """One frame from the middle of a video."""
    dur = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path], stdout=subprocess.PIPE
    ).stdout.decode().strip()
    try:
        t = float(dur) * at
    except ValueError:
        t = 0.0
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", path,
                    "-frames:v", "1", out], check=True)
    return out


def main():
    if len(sys.argv) < 5:
        print(__doc__)
        return 2
    src, ai, final, out = sys.argv[1:5]
    tmp = os.path.join(os.path.dirname(os.path.abspath(out)), "_frames")
    os.makedirs(tmp, exist_ok=True)
    parts = []
    for i, path in enumerate((src, ai, final)):
        if not os.path.exists(path):
            print(f"missing: {path}", file=sys.stderr)
            return 1
        p = frame(path, os.path.join(tmp, f"f{i}.png"))
        parts.append(p)
    # the same height for all three, so they can be laid side by side
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for p in parts:
        cmd += ["-i", p]
    cmd += ["-filter_complex",
            "[0:v]scale=-1:360,pad=iw:360:0:(oh-ih)/2:color=black[a];"
            "[1:v]scale=-1:360,pad=iw:360:0:(oh-ih)/2:color=black[b];"
            "[2:v]scale=-1:360,pad=iw:360:0:(oh-ih)/2:color=black[c];"
            "[a][b][c]hstack=inputs=3",
            out]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise SystemExit(r.stderr.decode()[-400:])
    for p in parts:
        os.remove(p)
    os.rmdir(tmp)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
