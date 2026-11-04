#!/usr/bin/env python3
"""Wire the SeedVR2 engine through the whole stack.

- cloud.py: send the output resolution as X-Resolution.
- engine.py: pass the target height to the worker.
- worker/server.py: run SeedVR2's standalone CLI, and route X-Engine.
- worker/Dockerfile: a build stage for SeedVR2.

SeedVR2 is the official ComfyUI release of ByteDance's SeedVR2 video
super-resolution model. It ships a standalone CLI that takes a video and
writes a video, so it fits the existing protocol without any new plumbing:
https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler
"""
import sys

ROOT = "/Users/vasilijemicic/upscaler-gui"


def patch(path, pairs):
    src = open(path, encoding="utf-8").read()
    for old, new, label in pairs:
        if old not in src:
            print("MISS:", path, label)
            continue
        if src.count(old) != 1:
            print("AMBIGUOUS (%d):" % src.count(old), path, label)
            continue
        src = src.replace(old, new)
        print("ok:", path, label)
    open(path, "w", encoding="utf-8").write(src)


# --- cloud.py -------------------------------------------------------------
patch(ROOT + "/cloud.py", [(
    '''def upscale(endpoint, video_path, out_path, scale=2, model=None, denoise=None,
            api_key=None, timeout=7200, log=print, engine="realesrgan"):''',
    '''def upscale(endpoint, video_path, out_path, scale=2, model=None, denoise=None,
            api_key=None, timeout=7200, log=print, engine="realesrgan",
            resolution=None):''',
    "upscale() takes a resolution"),
    (
    '''    if engine:
        headers["X-Engine"] = str(engine)''',
    '''    if engine:
        headers["X-Engine"] = str(engine)
    if resolution:
        # The diffusion engines work to an output resolution (720, 1080,
        # 1440) rather than to a factor.
        headers["X-Resolution"] = str(resolution)''',
    "the X-Resolution header"),
])

# --- engine.py ------------------------------------------------------------
patch(ROOT + "/engine.py", [(
    '''            cloud.upscale(endpoint, upload, ai, scale=scale, model=model,
                          api_key=opts.get("cloud_key") or None,
                          engine=engine,
                          log=lambda m: self.say(m))''',
    '''            cloud.upscale(endpoint, upload, ai, scale=scale, model=model,
                          api_key=opts.get("cloud_key") or None,
                          engine=engine,
                          resolution=(target[1] if target else 1080),
                          log=lambda m: self.say(m))''',
    "pass the resolution to the worker"),
])

# --- worker/server.py -----------------------------------------------------
patch(ROOT + "/worker/server.py", [
    (
    '''def dloral_upscale(video_path, out_path, scale=4, log=print):''',
    '''def seedvr2_upscale(video_path, out_path, resolution=1080, log=print):
    """SeedVR2: one-step diffusion video super-resolution.

    https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler
    (the official release of https://github.com/ByteDance-Seed/SeedVR)

    It ships a standalone CLI that takes a video and writes a video, so this
    is a thin wrapper. It runs on CUDA, and on Apple Silicon through MPS,
    but it wants a lot more memory than the Real-ESRGAN pass and it is much
    slower per frame - it is worth it on shots, not on a whole video.
    """
    repo = os.environ.get("SEEDVR2_REPO", "/app/seedvr2")
    if not os.path.isdir(repo):
        raise RuntimeError(
            "SeedVR2 is not installed on this box. Build the image with "
            "--build-arg BUILD_SEEDVR2=1.")
    work = os.path.dirname(video_path)
    outdir = os.path.join(work, "seedvr2_out")
    os.makedirs(outdir, exist_ok=True)
    cmd = ["python3", "inference_cli.py", video_path,
           "--output", outdir,
           "--resolution", str(resolution),
           "--batch_size", os.environ.get("SEEDVR2_BATCH", "33"),
           "--video_backend", "ffmpeg"]
    if os.environ.get("SEEDVR2_CHUNK"):
        cmd += ["--chunk_size", os.environ["SEEDVR2_CHUNK"],
                "--temporal_overlap", "3"]
    log(f"running SeedVR2 at {resolution}p…")
    res = run(cmd, cwd=repo)
    produced = []
    for root, _dirs, files in os.walk(outdir):
        for f in files:
            if f.lower().endswith((".mp4", ".mkv", ".mov", ".webm")):
                produced.append(os.path.join(root, f))
    if not produced:
        raise RuntimeError("SeedVR2 produced no video:\\n"
                           + res.stdout.decode("utf-8", "replace")[-800:])
    shutil.move(max(produced, key=os.path.getmtime), out_path)
    return out_path


def dloral_upscale(video_path, out_path, scale=4, log=print):''',
    "the SeedVR2 engine"),
    (
    '''            "engines": ["realesrgan", "dloral"]})''',
    '''            "engines": ["realesrgan", "seedvr2", "dloral"]})''',
    "the health endpoint lists the engines"),
    (
    '''        engine = (self.headers.get("X-Engine") or "realesrgan").lower()''',
    '''        engine = (self.headers.get("X-Engine") or "realesrgan").lower()
        resolution = int(self.headers.get("X-Resolution") or 1080)''',
    "read X-Resolution"),
    (
    '''            if engine == "dloral":''',
    '''            if engine == "seedvr2":
                seedvr2_upscale(src, out, resolution=resolution,
                                log=lambda m: print(m, flush=True))
            elif engine == "dloral":''',
    "route the seedvr2 engine"),
])

# --- worker/Dockerfile ----------------------------------------------------
patch(ROOT + "/worker/Dockerfile", [(
    '''ENV DLORAL_REPO=/app/DLoRAL
ENV DLORAL_MODELS=/app/DLoRAL/preset/models''',
    '''ENV DLORAL_REPO=/app/DLoRAL
ENV DLORAL_MODELS=/app/DLoRAL/preset/models

# ---------------------------------------------------------------------------
# SeedVR2 - one-step diffusion video super-resolution.
#
# The official ComfyUI release of ByteDance's SeedVR2. It has a standalone
# CLI, so it plugs into the same /upscale endpoint as everything else.
#
#   docker build --build-arg BUILD_SEEDVR2=1 -t upscaler:seedvr2 .
#
# The model weights come from Hugging Face on first run and land in the HF
# cache, so the first job on a cold box is slower.
# ---------------------------------------------------------------------------
ARG BUILD_SEEDVR2=0
RUN if [ "$BUILD_SEEDVR2" = "1" ]; then \\
        apt-get update && apt-get install -y --no-install-recommends git && \\
        rm -rf /var/lib/apt/lists/* && \\
        git clone --depth 1 https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler.git /app/seedvr2 && \\
        cd /app/seedvr2 && \\
        pip install --no-cache-dir -r requirements.txt && \\
        echo "SeedVR2 installed." ; \\
    else \\
        echo "built without SeedVR2 (use --build-arg BUILD_SEEDVR2=1 for that)" ; \\
    fi

ENV SEEDVR2_REPO=/app/seedvr2''',
    "the SeedVR2 build stage"),
])
print("done")
