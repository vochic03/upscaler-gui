#!/usr/bin/env python3
"""
STAR — Spatial-Temporal Augmentation with Text-to-Video Models (ICCV 2025).

    https://github.com/NJU-PCALab/STAR

What it is, in one paragraph: it is not a scaler. It is a *diffusion* model.
You give it a short, degraded clip and a sentence describing what is in the
clip, and it regenerates the clip at a higher resolution - inventing detail
that was never in the source, but keeping the invented detail consistent
between neighbouring frames so the result does not shimmer. The text prompt
is what steers it.

Running it: the official code needs an NVIDIA GPU with 24 GB+ of VRAM
(CUDA, `xformers`, `torch==2.0.1`). An Apple Silicon Mac has neither CUDA
nor that much memory, so the only way to use it from this machine is the
official Hugging Face Space, which runs on a free ZeroGPU A10G.

That has three consequences the GUI makes obvious:

  * the clip is uploaded to huggingface.co - only do this with footage you
    are allowed to upload;
  * the input has to be small. The Space's own example is 320x240; the GPU
    budget is 100 seconds per call, and the model works in 24-frame chunks;
  * the result is a *generation*, not a faithful enlargement. It is good at
    making low quality footage look plausible. It is not the right tool for
    footage that is already clean.
"""

import ffbin
import os
import shutil
import subprocess
import time

SPACE = "SherryX/STAR"
ENDPOINT = "/enhance_with_gpu"

# The official Space first, then the two community mirrors of it. Each one
# runs on a free ZeroGPU A10G, and any of them can be cold, broken or out of
# GPU quota, so the client tries them in turn.
SPACES = [
    "SherryX/STAR",
    "multimodalart/STAR-zerogpu",
    "O-ken5481/STAR-UpSucale",
]

# The Space's own examples are 320x240 at 25 fps.
DEFAULT_WIDTH = 320
DEFAULT_FPS = 25
DEFAULT_SECONDS = 2.0


def available():
    """Is the cloud route usable from here?"""
    try:
        import gradio_client  # noqa: F401
    except ImportError:
        return False, "gradio_client is not installed (pip install gradio_client)"
    return True, "ok"


def space_status():
    """Ask huggingface.co which of the Spaces are alive."""
    import json
    import urllib.request
    out = []
    for name in SPACES:
        entry = {"space": name}
        try:
            with urllib.request.urlopen(
                    "https://huggingface.co/api/spaces/" + name, timeout=20) as r:
                d = json.load(r)
            rt = d.get("runtime") or {}
            entry["stage"] = rt.get("stage")
            entry["running"] = rt.get("stage") == "RUNNING"
            entry["hardware"] = (rt.get("hardware") or {}).get("current")
        except Exception as e:
            entry["stage"] = "unreachable"
            entry["running"] = False
            entry["error"] = str(e)[:120]
        out.append(entry)
    return out


def prepare_clip(src, dst, start=0.0, seconds=DEFAULT_SECONDS,
                 width=DEFAULT_WIDTH, fps=DEFAULT_FPS):
    """Cut a short, small, silent mp4 that the Space can handle."""
    vf = f"scale={int(width)}:-2:flags=lanczos,fps={int(fps)}"
    cmd = [ffbin.ffmpeg(), "-y", "-v", "error",
           "-ss", f"{float(start):.3f}", "-t", f"{float(seconds):.3f}",
           "-i", src, "-vf", vf,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-pix_fmt", "yuv420p", "-an", dst]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0 or not os.path.exists(dst):
        raise RuntimeError("could not prepare the clip: "
                           + res.stderr.decode("utf-8", "replace")[-400:])
    return dst


def preflight():
    """What is the cloud route worth trying right now?

    Returns (worth_trying, lines). The point is to tell the user *before*
    their clip is uploaded, because uploading to a service that is going to
    refuse the job is pointless.
    """
    ok, why = available()
    if not ok:
        return False, [why]
    lines = []
    for entry in space_status():
        if entry["running"]:
            lines.append(f"{entry['space']}: up, on {entry.get('hardware')}")
        else:
            lines.append(f"{entry['space']}: {entry.get('stage')}")
    return True, lines


def _predict(client, src_clip, prompt, upscale, chunk_len, chunk_size):
    # Ask the space what it actually wants, so a mirror with slightly
    # different inputs still works.
    try:
        api = client.view_api(return_format="dict")
        wanted = {}
        for ep in api.get("named_endpoints", []):
            for f in ep.get("parameters", []):
                wanted[f["parameter_name"]] = f.get("parameter_default")
    except Exception:
        wanted = {}

    def pick(*names, default=None):
        for n in names:
            if n in wanted:
                return n
        return names[0] if names else default

    video_key = pick("input_video", "video", "input")
    text_key = pick("input_text", "text", "prompt")
    up_key = pick("upscale", "upscale_factor", "scale")
    len_key = pick("max_chunk_len", "chunk_len", "max_chunk_length")
    size_key = pick("chunk_size", "decode_chunk_size")

    kwargs = {
        video_key: src_clip,
        text_key: prompt,
        up_key: int(upscale),
    }
    if len_key:
        kwargs[len_key] = int(chunk_len)
    if size_key:
        kwargs[size_key] = int(chunk_size)

    result = client.predict(api_name=ENDPOINT, **kwargs)
    path = result
    if isinstance(result, dict):
        path = result.get("video") or result.get("path")
    if isinstance(result, (list, tuple)):
        path = result[0]
    if not path or not os.path.exists(path):
        raise RuntimeError(f"the space returned no video: {result!r}")
    return path


def run(src_clip, out_path, prompt, upscale=4, chunk_len=24, chunk_size=3,
        token=None, log=print, timeout=1800):
    """Upload the clip to a Space, run it, bring the result back.

    The Spaces are tried in turn: they are free ZeroGPU instances, and one
    of them being cold, broken or out of quota should not stop the job.
    """
    from gradio_client import Client

    errors = []
    for name in SPACES:
        log(f"trying the {name} space…")
        try:
            client = Client(name, token=token or None, verbose=False)
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:200]}")
            continue
        t0 = time.time()
        try:
            path = _predict(client, src_clip, prompt, upscale, chunk_len, chunk_size)
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:200]}")
            continue
        log(f"{name} answered in {time.time() - t0:.0f}s")
        shutil.copy(path, out_path)
        return out_path
    raise RuntimeError("no STAR space could take the job:\n  " + "\n  ".join(errors))


DEFAULT_PROMPT = ("A television broadcast. The video shows a presenter in a "
                  "studio, with clean, sharp detail and natural colour.")

# What the state of the public demos actually is, in plain words. The
# maintainers say it themselves in the pinned discussion on the Space:
#
#   "Due to the duration limitations of ZeroGPU, the running time may exceed
#    the allocated GPU duration. If you'd like to give it a try, you can
#    duplicate the demo and assign a paid GPU for extended use."
#
# and a second maintainer:
#
#   "Since the inference time will exceed the ZeroGPU limitation, you could
#    try to duplicate the updated version running with your paid GPU"
#
# ZeroGPU gives an anonymous visitor 2 minutes of GPU a day and a free
# account 5. One STAR call asks for 100 seconds. Users report it erroring
# even on a paid single GPU, and working on 1x L40S + 1x L4.
#
# So the honest answer for someone on an Apple Silicon Mac: the local code
# needs CUDA and 24-39 GB of VRAM, and the public demos run out of GPU
# budget before they finish. The integration is here and it is correct; the
# remote end is what is missing.
DEMO_NOTE = (
    "STAR's official demos are out of GPU budget: ZeroGPU gives an anonymous "
    "visitor 2 minutes of GPU a day and a free account 5, and one STAR call "
    "asks for 100 seconds. The authors say so themselves - they suggest "
    "duplicating the Space onto a paid GPU. So this usually fails at the far "
    "end, not in your Mac. If you have a HuggingFace token, paste it below "
    "for more quota; for a guaranteed run, use star_remote.sh on a rented "
    "NVIDIA GPU (L40S or L4)."
)
