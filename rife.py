#!/usr/bin/env python3
"""
Frame interpolation - making the motion smoother.

RIFE (Real-Time Intermediate Flow Estimation) invents the frames *between*
the ones you have, so 25 fps PAL footage becomes 50 fps and the judder goes
away. It is the same ncnn-vulkan shape as the Real-ESRGAN binary, so it runs
on the Apple GPU the same way, and it is a small download compared to
everything else.

From the upstream notes, the workflow is: decode the frames, run the model
over the directory, encode the result at the new rate.

4x is two 2x passes - that is how the time-step ladder works, and it gives
better results than trying to jump straight to 4x.
"""

import json
import os
import shutil
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RIFE_DIR = os.environ.get("RIFE_DIR",
                           os.path.join(HERE, "vendor", "rife",
                                        "rife-ncnn-vulkan-20221029-macos"))
RIFE_BIN = os.environ.get("RIFE_BIN", os.path.join(RIFE_DIR, "rife-ncnn-vulkan"))

# The model folders that ship with the release.
MODELS = ["rife-v4.6", "rife-v4", "rife-v3.1", "rife-v3.0", "rife-v2.4", "rife-v2.3",
          "rife-HD", "rife", "rife-UHD", "rife-anime"]


def available():
    return os.path.exists(RIFE_BIN)


def model_path(name=None):
    """A model folder that actually exists in the release."""
    if name:
        p = os.path.join(RIFE_DIR, name)
        if os.path.isdir(p):
            return p
    for m in MODELS:
        p = os.path.join(RIFE_DIR, m)
        if os.path.isdir(p):
            return p
    return RIFE_DIR


def _run(cmd, **kw):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)


def interpolate(src, dst, factor=2, model=None, fps=None, log=print):
    """Interpolate `src` by `factor` and write it to `dst`.

    The audio is carried over untouched.
    """
    if not available():
        raise RuntimeError("the RIFE binary is not here: " + RIFE_BIN)
    if factor not in (2, 3, 4):
        raise RuntimeError(f"RIFE can make 2x, 3x or 4x, not {factor}x")

    work = tempfile.mkdtemp(prefix="rife-")
    try:
        frames_in = os.path.join(work, "in")
        frames_out = os.path.join(work, "out")
        os.makedirs(frames_in)
        os.makedirs(frames_out)

        info = json.loads(_run(["ffprobe", "-v", "error", "-print_format", "json",
                                "-show_streams", "-show_format", src]).stdout.decode())
        v = next(s for s in info["streams"] if s.get("codec_type") == "video")
        src_fps = fps
        if not src_fps:
            try:
                num, den = (v.get("avg_frame_rate") or "25/1").split("/")
                src_fps = float(num) / float(den) if float(den) else 25.0
            except Exception:
                src_fps = 25.0
        duration = float(info.get("format", {}).get("duration") or 0)
        total = int(round(duration * src_fps)) or 1

        log(f"decoding {total} frames at {src_fps:.3f} fps")
        _run(["ffmpeg", "-y", "-v", "error", "-i", src,
              os.path.join(frames_in, "%08d.png")])
        have = len([f for f in os.listdir(frames_in) if f.endswith(".png")])
        if have < 2:
            raise RuntimeError("there are not enough frames to interpolate")

        # 4x is two 2x passes; 3x is a 2x pass and then a 1.5x one.
        passes = {2: [0.5], 3: [0.5, 1.0 / 3.0], 4: [0.5, 0.5]}[factor]
        cur_in = frames_in
        for i, step in enumerate(passes):
            cur_out = frames_out if i == len(passes) - 1 else os.path.join(work, f"out{i}")
            os.makedirs(cur_out, exist_ok=True)
            # After pass i the frame count should be have * F^((i+1)/P):
            # half the total factor done, for the 2x/2x split that is 4^0.5.
            target = int(round(have * (factor ** ((i + 1) / len(passes)))))
            log(f"RIFE pass {i + 1}/{len(passes)}: time step {step:.3f}, "
                f"target {target} frames")
            res = _run([RIFE_BIN, "-i", cur_in, "-o", cur_out,
                        "-s", f"{step:.4f}", "-n", str(target),
                        "-m", model_path(model), "-f", "%08d.png"])
            got = len([f for f in os.listdir(cur_out) if f.endswith(".png")])
            if not got:
                raise RuntimeError("RIFE produced nothing:\n"
                                   + res.stdout.decode("utf-8", "replace")[-600:])
            cur_in = cur_out

        new_fps = src_fps * factor
        log(f"encoding at {new_fps:.3f} fps")
        cmd = ["ffmpeg", "-y", "-v", "error", "-framerate", f"{new_fps:.6f}",
               "-i", os.path.join(frames_out, "%08d.png"), "-i", src,
               "-map", "0:v", "-map", "1:a?", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
               "-r", f"{new_fps:.6f}", dst]
        res = _run(cmd)
        if res.returncode != 0 or not os.path.exists(dst):
            raise RuntimeError("could not encode the interpolated video:\n"
                               + res.stdout.decode("utf-8", "replace")[-600:])
        return dst
    finally:
        shutil.rmtree(work, ignore_errors=True)
