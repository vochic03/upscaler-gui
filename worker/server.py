#!/usr/bin/env python3
"""
The GPU worker: the half of the job that needs a rented NVIDIA GPU.

    POST /upscale        the video in, the upscaled video out
    GET  /health         what this box is and what it can do

Everything else - the denoise, the blend with the plain enlargement, the
softening, the grain, the grade and the final encode - stays on your Mac,
because that is all cheap CPU work and there is no reason to spend GPU
seconds on it.

The point of the split: the AI pass is the only part that wants a big GPU,
and it is the only part that is worth paying for. Your Mac then does what it
is good at and stays cool.

The protocol is deliberately plain so it can sit behind any of the rental
services:

    POST /upscale
      body: the video, either raw (Content-Type: video/mp4) or as the
            "video" field of a multipart form
      headers (all optional):
        X-Scale:   2 | 3 | 4
        X-Model:   the model name, e.g. xintao-realesr-animevideo-x2-v3
        X-Denoise: an ffmpeg filter, e.g. hqdn3d=3.5:2.5:6:4
      200, the upscaled video as the body

Runs on the standard library only, so the container image stays tiny.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.environ.get("UPSCALER_BIN", "/app/bin/realesrgan-ncnn-vulkan")
MODELS = os.environ.get("UPSCALER_MODELS", "/app/models")
PORT = int(os.environ.get("PORT", "8000"))
CHUNK = int(os.environ.get("CHUNK_FRAMES", "40"))

# The PyTorch path, for when there is a card and torch to drive it.
#
# The ncnn binary needs Vulkan, and the rental boxes hand out a CUDA stack
# whose Vulkan ICD refuses to give a device to anyone ("invalid gpu
# device"). CUDA they do hand out, so on a rented box the AI pass runs
# through PyTorch instead - the same weights, the same job. On the laptop
# there is no torch and no card, so the ncnn binary does the work there.
try:
    sys.path.insert(0, HERE)
    import realesrgan_cuda
    CUDA = realesrgan_cuda.cuda_available()
except Exception:
    realesrgan_cuda = None
    CUDA = False

# A model id -> the file name the binary wants.
MODELS_DIR = MODELS

# The factor each model was trained at. The NCNN Real-ESRGAN binary only
# tiles correctly at the model's own scale: asked for another -s it emulates
# it by tiling the input, and the tiles do not line up, so the output comes
# back as a patchwork of blocks at different magnifications.
NATIVE_SCALES = {
    "xintao-realesr-animevideo-x2-v3": 2,
    "xintao-realesr-animevideo-x3-v3": 3,
    "xintao-realesr-animevideo-x4-v3": 4,
    "upscayl-realesrgan-x4plus": 4,
    "upscayl-realesrgan-x4plus-v3": 4,
    "upscayl-realesrgan-x4plus-anime": 4,
    "upscayl-ultrasharp": 4,
    "upscayl-ultrasharp-v2": 4,
    "upscayl-remacri": 4,
    "upscayl-ultramix_balanced": 4,
    "xintao-realesrgan-x4plus": 4,
    "xintao-realesrgan-x4plus-anime": 4,
}


def scale_for(model, scale):
    """The factor to run the model at: its own, whatever was asked for."""
    native = NATIVE_SCALES.get(model)
    if native and scale != native:
        print(f"model {model} runs at {native}x natively; using that instead "
              f"of {scale}x", flush=True)
        return native
    return scale


def gpu_name():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total",
                              "--format=csv,noheader"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=15)
        return out.stdout.decode().strip() or "no GPU visible"
    except Exception:
        return "no GPU visible"


def run(cmd, **kw):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)


def upscale(video_path, out_path, scale=2, model=None, denoise=None, log=print):
    """The AI pass: frames out of the video, through the model, back to video."""
    probe = json.loads(run(["ffprobe", "-v", "error", "-print_format", "json",
                            "-show_streams", "-show_format", video_path]).stdout.decode())
    v = next(s for s in probe["streams"] if s.get("codec_type") == "video")
    fps = 0.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            num, den = (v.get(key) or "0/0").split("/")
            if float(den):
                fps = float(num) / float(den)
                break
        except Exception:
            pass
    fps = fps or 25.0
    scale = scale_for(model, scale)
    w = int(v.get("width") or 0) * scale // 2 * 2
    h = int(v.get("height") or 0) * scale // 2 * 2
    log(f"upscale {v.get('width')}x{v.get('height')} -> {w}x{h} at {fps:.3f} fps, "
        f"model {model or 'default'}")

    work = tempfile.mkdtemp(prefix="upscale-")
    frames_in = os.path.join(work, "in")
    frames_out = os.path.join(work, "out")
    os.makedirs(frames_in)
    os.makedirs(frames_out)
    try:
        # Count the frames first, so the progress can be a percentage.
        res = run(["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
                   "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", video_path])
        try:
            total = int((res.stdout.decode().strip() or "0").split(",")[0])
        except Exception:
            total = 0
        if not total:
            res = run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                       "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", video_path])
            try:
                total = int((res.stdout.decode().strip() or "0").split(",")[0])
            except Exception:
                total = 1

        done = 0
        first = 0
        t0 = time.time()
        # Extract every frame first, then run the model ONCE over the whole
        # directory: one process means the model is loaded once, and mp4
        # cannot be stitched together by appending bytes, so one encode at
        # the end is the only clean way to do it.
        while first < total:
            count = min(500, total - first)
            extract = ["ffmpeg", "-y", "-v", "error", "-ss", f"{first / fps:.4f}",
                       "-i", video_path, "-frames:v", str(count)]
            if denoise:
                extract += ["-vf", denoise]
            extract += ["-start_number", str(first + 1), "-f", "image2",
                        os.path.join(frames_in, "%06d.png")]
            run(extract)
            got = len([f for f in os.listdir(frames_in) if f.endswith(".png")])
            if got == 0:
                break
            first += got
            done = len([f for f in os.listdir(frames_in) if f.endswith(".png")])
            log(f"extracted {done}/{total} frames")
        if not done:
            raise RuntimeError("no frames could be extracted from that video")

        log("running the model…")
        cmd = [BIN, "-i", frames_in, "-o", frames_out, "-s", str(scale), "-m", MODELS_DIR]
        if model:
            cmd += ["-n", model]
        res = run(cmd)
        upscaled = sorted(f for f in os.listdir(frames_out) if f.endswith(".png"))
        if not upscaled:
            raise RuntimeError("the model produced no frames:\n"
                               + res.stdout.decode("utf-8", "replace")[-600:])
        log(f"model finished: {len(upscaled)} frames in {time.time() - t0:.0f}s")

        log("encoding…")
        cmd = ["ffmpeg", "-y", "-v", "error", "-framerate", f"{fps:.6f}",
               "-i", os.path.join(frames_out, "%06d.png"),
               "-i", video_path, "-map", "0:v", "-map", "1:a?", "-c:a", "copy",
               "-c:v", "libx264", "-crf", "16", "-preset", "medium",
               "-pix_fmt", "yuv420p", "-r", f"{fps:.6f}", "-movflags", "+faststart",
               out_path]
        res = run(cmd)
        if res.returncode != 0 or not os.path.exists(out_path):
            raise RuntimeError("encoding failed:\n"
                               + res.stdout.decode("utf-8", "replace")[-600:])
        return out_path
    finally:
        shutil.rmtree(work, ignore_errors=True)


def star_upscale(video_path, out_path, prompt="", scale=4, steps=15, chunk=32,
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
        raise RuntimeError("STAR produced no video:\n"
                           + res.stdout.decode("utf-8", "replace")[-800:])
    shutil.move(produced, out_path)
    return out_path


def seedvr2_upscale(video_path, out_path, resolution=1080, log=print):
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
        raise RuntimeError("SeedVR2 produced no video:\n"
                           + res.stdout.decode("utf-8", "replace")[-800:])
    shutil.move(max(produced, key=os.path.getmtime), out_path)
    return out_path


def dloral_upscale(video_path, out_path, scale=4, log=print):
    """The diffusion engine: DLoRAL, one-step diffusion video SR.

    https://github.com/yjsunnn/DLoRAL (NeurIPS 2025)

    It takes a *directory* of mp4 files and writes the enhanced frames as
    PNGs into <output>/<video name>/frame_%04d.png. The frames have to be
    put back into a video afterwards, which is what this does.

    It needs CUDA - the script calls .cuda() on the uncertainty map, so it
    cannot run on an Apple GPU.
    """
    repo = os.environ.get("DLORAL_REPO", "/app/DLoRAL")
    models = os.environ.get("DLORAL_MODELS", "/app/DLoRAL/preset/models")
    if not os.path.isdir(repo):
        raise RuntimeError(
            "DLoRAL is not installed on this box. It needs a CUDA GPU and its "
            "own stack (diffusers 0.25, torch 2.0.1, xformers, mmedit); see "
            "the Dockerfile's build-dloral argument.")
    if not os.path.exists(os.path.join(models, "checkpoints", "model.pkl")):
        raise RuntimeError(
            "the DLoRAL weights (model.pkl) are missing from " + models)
    sd = os.environ.get("DLORAL_SD", "yujingsun/stable-diffusion-2-1-base")
    cmd = ["python3", "src/test_DLoRAL.py",
           "--pretrained_model_path", sd,
           "--pretrained_model_name_or_path", sd,
           "--ram_path", os.path.join(models, "ram_swin_large_14m.pth"),
           "--ram_ft_path", os.path.join(models, "DAPE.pth"),
           "--pretrained_path", os.path.join(models, "checkpoints", "model.pkl"),
           "--process_size", "512",
           "--upscale", str(scale),
           "--load_cfr",
           "--stages", "1",
           "--merge_and_unload_lora", "False",
           "-i", os.path.dirname(video_path),
           "-o", os.path.join(os.path.dirname(video_path), "dloral_out")]
    log("running DLoRAL…")
    res = run(cmd, cwd=repo)
    stem = os.path.splitext(os.path.basename(video_path))[0]
    frames = os.path.join(os.path.dirname(video_path), "dloral_out", stem)
    pngs = sorted(f for f in os.listdir(frames)) if os.path.isdir(frames) else []
    if not pngs:
        raise RuntimeError("DLoRAL produced no frames:\n"
                           + res.stdout.decode("utf-8", "replace")[-800:])
    log(f"DLoRAL produced {len(pngs)} frames")

    # DLoRAL numbers its frames from 1 and drops the last one, so they are
    # renumbered into one contiguous sequence before they go back into a
    # video. ffmpeg's image2 demuxer needs a printf pattern, not "%s".
    seq = os.path.join(os.path.dirname(video_path), "dloral_seq")
    os.makedirs(seq, exist_ok=True)
    for i, name in enumerate(pngs, start=1):
        dst = os.path.join(seq, f"frame_{i:06d}.png")
        if not os.path.exists(dst):
            shutil.copy(os.path.join(frames, name), dst)
    pngs = [f"frame_{i:06d}.png" for i in range(1, len(pngs) + 1)]

    fps = 25.0
    probe = json.loads(run(["ffprobe", "-v", "error", "-print_format", "json",
                            "-show_streams", video_path]).stdout.decode())
    v = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {})
    try:
        num, den = (v.get("avg_frame_rate") or "25/1").split("/")
        if float(den):
            fps = float(num) / float(den)
    except Exception:
        pass

    res = run(["ffmpeg", "-y", "-v", "error", "-framerate", f"{fps:.6f}",
               "-i", os.path.join(seq, "frame_%06d.png"), "-i", video_path,
               "-map", "0:v", "-map", "1:a?", "-c:v", "libx264", "-crf", "16",
               "-preset", "medium", "-pix_fmt", "yuv420p", "-r", f"{fps:.6f}",
               "-c:a", "copy", "-movflags", "+faststart", out_path])
    if res.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError("could not put the DLoRAL frames back into a video:\n"
                           + res.stdout.decode("utf-8", "replace")[-600:])
    return out_path


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"{time.strftime('%H:%M:%S')}  {fmt % args}", flush=True)

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", ""):
            models = []
            if os.path.isdir(MODELS_DIR):
                models = sorted(f[:-6] for f in os.listdir(MODELS_DIR)
                                if f.endswith(".param"))
            self._json({"ok": True, "gpu": gpu_name(),
                        "models": models, "binary": os.path.exists(BIN),
                        "engines": ["realesrgan", "seedvr2", "star", "dloral"],
                        "backend": "cuda" if CUDA else "ncnn",
                        "cuda_models": (realesrgan_cuda.list_models()
                                        if realesrgan_cuda else [])})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path.rstrip("/") != "/upscale":
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length else b""
        if not data:
            self._json({"error": "empty body"}, 400)
            return
        scale = int(self.headers.get("X-Scale") or 2)
        model = self.headers.get("X-Model") or None
        denoise = self.headers.get("X-Denoise") or None
        engine = (self.headers.get("X-Engine") or "realesrgan").lower()
        resolution = int(self.headers.get("X-Resolution") or 1080)
        prompt = (self.headers.get("X-Prompt") or "").strip()

        work = tempfile.mkdtemp(prefix="job-")
        try:
            src = os.path.join(work, "in.mp4")
            with open(src, "wb") as fh:
                fh.write(data)
            out = os.path.join(work, "out.mp4")
            t0 = time.time()
            if engine == "star":
                star_upscale(src, out, prompt=prompt, scale=scale or 4,
                             steps=int(os.environ.get("STAR_STEPS", "15")),
                             chunk=int(os.environ.get("STAR_CHUNK", "32")),
                             log=lambda m: print(m, flush=True))
            elif engine == "seedvr2":
                seedvr2_upscale(src, out, resolution=resolution,
                                log=lambda m: print(m, flush=True))
            elif engine == "dloral":
                # DLoRAL wants a directory of videos.
                vids = os.path.join(work, "videos")
                os.makedirs(vids)
                clip = os.path.join(vids, "clip.mp4")
                shutil.copy(src, clip)
                dloral_upscale(clip, out, scale=scale or 4, log=lambda m: print(m, flush=True))
            else:
                # Real-ESRGAN. On a rented box that means PyTorch on the
                # card; on the laptop it means the ncnn binary.
                cuda_model = bool(CUDA and realesrgan_cuda
                                  and realesrgan_cuda.has_model(model))
                if engine == "realesrgan-cuda" and not cuda_model:
                    raise RuntimeError(
                        "this box has no CUDA weights for that model "
                        f"(have: {realesrgan_cuda.list_models() if realesrgan_cuda else []})")
                if cuda_model:
                    realesrgan_cuda.upscale(src, out, model=model, scale=scale,
                                           denoise=denoise,
                                           log=lambda m: print(m, flush=True))
                else:
                    upscale(src, out, scale=scale, model=model, denoise=denoise)
            size = os.path.getsize(out)
            print(f"job done: {size/1e6:.1f} MB in {time.time()-t0:.0f}s", flush=True)
            with open(out, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        finally:
            shutil.rmtree(work, ignore_errors=True)


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"upscaler worker on :{PORT}  gpu={gpu_name()}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
