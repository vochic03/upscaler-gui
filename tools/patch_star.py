#!/usr/bin/env python3
"""Wire STAR into the upscaler.

STAR (NJU-PCALab) re-synthesises the missing detail with a text-to-video
prior, so the prompt is a real control: it decides what the upscale invents.
That is the difference between "bigger" and "this look" - and it is the knob
that turns the fixed DVD recipe into something you can steer.

    https://github.com/NJU-PCALab/STAR

The prompt travels GUI -> engine -> cloud.upscale -> X-Prompt -> worker.
"""
import sys

ROOT = "/Users/vasilijemicic/upscaler-gui"
ok = True


def patch(path, pairs):
    global ok
    src = open(path, encoding="utf-8").read()
    for old, new, label in pairs:
        if old not in src:
            print("MISS:", path, "-", label)
            ok = False
            continue
        if src.count(old) != 1:
            print("AMBIGUOUS (%d):" % src.count(old), path, "-", label)
            ok = False
            continue
        src = src.replace(old, new)
        print("ok:", path, "-", label)
    open(path, "w", encoding="utf-8").write(src)


# --- worker/server.py -----------------------------------------------------
patch(ROOT + "/worker/server.py", [
    (
    '''def seedvr2_upscale(video_path, out_path, resolution=1080, log=print):''',
    '''def star_upscale(video_path, out_path, prompt="", scale=4, steps=15, chunk=32,
                 log=print):
    """STAR: text-prompt-guided video super-resolution.

    https://github.com/NJU-PCALab/STAR

    It re-synthesises the missing detail with a text-to-video prior, so the
    prompt is a real control: it steers what the upscale invents. That is
    the difference between "bigger" and "this look".

    It wants a lot of memory: about 39 GB of VRAM for a 4x upscale of 72
    frames with the default settings. Lower the chunk length if the card is
    smaller.
    """
    repo = os.environ.get("STAR_REPO", "/app/STAR")
    model = os.environ.get("STAR_MODEL", "/app/STAR/pretrained_weight/model.pt")
    if not os.path.isdir(repo):
        raise RuntimeError(
            "STAR is not installed on this box. Build the image with "
            "--build-arg BUILD_STAR=1.")
    if not os.path.exists(model):
        raise RuntimeError(
            "The STAR weights are not in the image. They live on Hugging Face "
            "at SherryX/STAR (light_deg.pt or heavy_deg.pt).")
    work = os.path.dirname(video_path)
    save_dir = os.path.join(work, "star_results")
    os.makedirs(save_dir, exist_ok=True)
    cmd = ["python3", "video_super_resolution/scripts/inference_sr.py",
           "--input_path", video_path,
           "--save_dir", save_dir,
           "--file_name", "star_out.mp4",
           "--model_path", model,
           "--upscale", str(scale),
           "--max_chunk_len", str(chunk),
           "--steps", str(steps),
           "--solver_mode", "fast"]
    if prompt:
        cmd += ["--prompt", prompt]
    log(f"running STAR at {scale}x, {steps} steps"
        f"{' with a prompt' if prompt else ''}...")
    res = run(cmd, cwd=repo)
    produced = os.path.join(save_dir, "star_out.mp4")
    if not os.path.exists(produced):
        raise RuntimeError("STAR produced no video:\\n"
                           + res.stdout.decode("utf-8", "replace")[-800:])
    shutil.move(produced, out_path)
    return out_path


def seedvr2_upscale(video_path, out_path, resolution=1080, log=print):''',
    "the STAR engine"),
    (
    '''        resolution = int(self.headers.get("X-Resolution") or 1080)''',
    '''        resolution = int(self.headers.get("X-Resolution") or 1080)
        prompt = (self.headers.get("X-Prompt") or "").strip()''',
    "read X-Prompt"),
    (
    '''            if engine == "seedvr2":''',
    '''            if engine == "star":
                star_upscale(src, out, prompt=prompt, scale=scale or 4,
                             steps=int(os.environ.get("STAR_STEPS", "15")),
                             chunk=int(os.environ.get("STAR_CHUNK", "32")),
                             log=lambda m: print(m, flush=True))
            elif engine == "seedvr2":''',
    "route the star engine"),
    (
    '''            "engines": ["realesrgan", "seedvr2", "dloral"]})''',
    '''            "engines": ["realesrgan", "seedvr2", "star", "dloral"]})''',
    "the health endpoint lists the engines"),
])

# --- cloud.py -------------------------------------------------------------
patch(ROOT + "/cloud.py", [
    (
    '''            api_key=None, timeout=7200, log=print, engine="realesrgan",
            resolution=None):''',
    '''            api_key=None, timeout=7200, log=print, engine="realesrgan",
            resolution=None, prompt=None):''',
    "upscale() takes a prompt"),
    (
    '''    if resolution:
        # The diffusion engines work to an output resolution (720, 1080,
        # 1440) rather than to a factor.
        headers["X-Resolution"] = str(resolution)''',
    '''    if resolution:
        # The diffusion engines work to an output resolution (720, 1080,
        # 1440) rather than to a factor.
        headers["X-Resolution"] = str(resolution)
    if prompt:
        # The prompt steers what the diffusion engine invents.
        headers["X-Prompt"] = prompt''',
    "the X-Prompt header"),
])

# --- engine.py ------------------------------------------------------------
patch(ROOT + "/engine.py", [
    (
    '''                          engine=engine,
                          resolution=(target[1] if target else 1080),
                          log=lambda m: self.say(m))''',
    '''                          engine=engine,
                          resolution=(target[1] if target else 1080),
                          prompt=opts.get("prompt") or None,
                          log=lambda m: self.say(m))''',
    "pass the prompt to the worker"),
])

# --- server.py ------------------------------------------------------------
patch(ROOT + "/server.py", [
    (
    '''                "cloud_engine": body.get("cloud_engine") or "realesrgan",''',
    '''                "cloud_engine": body.get("cloud_engine") or "realesrgan",
                # The direction, for the engines that can be steered.
                "prompt": body.get("prompt") or "",''',
    "read the prompt from the request"),
])

# --- index.html -----------------------------------------------------------
patch(ROOT + "/index.html", [
    (
    '''      <select id="cloudEngine">
        <option value="realesrgan">Real-ESRGAN — enlarge, faithful</option>
        <option value="dloral">DLoRAL — one-step diffusion, temporally consistent</option>
      </select>''',
    '''      <select id="cloudEngine">
        <option value="realesrgan">Real-ESRGAN — enlarge, faithful</option>
        <option value="seedvr2">SeedVR2 — one-step diffusion, fast</option>
        <option value="star">STAR — prompt-guided diffusion, steerable</option>
        <option value="dloral">DLoRAL — one-step diffusion, temporally consistent</option>
      </select>
      <div id="directionRow">
        <label>Direction — what should the upscale invent?</label>
        <input type="text" id="direction" spellcheck="false"
               placeholder="1990s DVD player, soft analog bloom, gentle film grain, slightly faded colour">
        <p class="hint">Only the prompt-guided engines use this. It is not a
        filter description: it is the texture and the mood the model aims at
        when it rebuilds the missing detail.</p>
      </div>''',
    "the engine list and the direction field"),
    (
    '''  $('cloudEngine').onchange = function () {
    $('cloudPriceRow').style.display = $('cloudEngine').value === 'dloral' ? 'none' : '';
    refreshRentGpus();
  };''',
    '''  $('cloudEngine').onchange = function () {
    $('cloudPriceRow').style.display = $('cloudEngine').value === 'dloral' ? 'none' : '';
    $('directionRow').style.display = $('cloudEngine').value === 'star' ? '' : 'none';
    refreshRentGpus();
  };''',
    "show the direction field for STAR"),
    (
    '''      body.cloud_engine = $('cloudEngine').value;''',
    '''      body.cloud_engine = $('cloudEngine').value;
      body.prompt = $('direction').value.trim();''',
    "send the prompt"),
])

# --- worker/Dockerfile ----------------------------------------------------
patch(ROOT + "/worker/Dockerfile", [
    (
    '''ENV SEEDVR2_REPO=/app/seedvr2''',
    '''ENV SEEDVR2_REPO=/app/seedvr2

# ---------------------------------------------------------------------------
# STAR - text-prompt-guided video super-resolution.
#
# It rebuilds the missing detail with a text-to-video prior, so the prompt
# decides what gets invented. The weights are on Hugging Face (SherryX/STAR),
# so the image can be built without a manual download.
#
#   docker build --build-arg BUILD_STAR=1 -t upscaler:star .
#
# STAR wants a big card: about 39 GB of VRAM for 4x on 72 frames. Lower
# STAR_CHUNK if the card is smaller.
# ---------------------------------------------------------------------------
ARG BUILD_STAR=0
RUN if [ "$BUILD_STAR" = "1" ]; then \\
        apt-get update && apt-get install -y --no-install-recommends git curl && \\
        rm -rf /var/lib/apt/lists/* && \\
        git clone --depth 1 https://github.com/NJU-PCALab/STAR.git /app/STAR && \\
        cd /app/STAR && \\
        pip install --no-cache-dir -r requirements.txt && \\
        mkdir -p pretrained_weight && \\
        curl -L --fail -o pretrained_weight/model.pt \\
          "https://huggingface.co/SherryX/STAR/resolve/main/I2VGen-XL-based/light_deg.pt?download=true" && \\
        echo "STAR installed." ; \\
    else \\
        echo "built without STAR (use --build-arg BUILD_STAR=1 for that)" ; \\
    fi

ENV STAR_REPO=/app/STAR
ENV STAR_MODEL=/app/STAR/pretrained_weight/model.pt''',
    "the STAR build stage"),
])

print("all patches applied" if ok else "SOME PATCHES MISSED")
sys.exit(0 if ok else 1)
