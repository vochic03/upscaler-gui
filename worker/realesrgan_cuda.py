"""Real-ESRGAN on the GPU, through PyTorch.

The rented boxes have a working CUDA stack, but their Vulkan ICD refuses to
hand a device to the ncnn build - `vkCreateInstance` comes back empty and
the model says "invalid gpu device". So on the card we run the same weights
through PyTorch instead, and the ncnn binary stays for the laptop.

The frames never touch the disk: ffmpeg decodes into a pipe, the model
reads from that pipe, and the result is written into a second pipe that
ffmpeg encodes from. A long clip costs no PNG round-trip at all, which is
what used to push a request past the proxy's time limit.
"""
import json
import os
import subprocess
import time

import numpy as np
import torch

from arch_models import RRDBNet, SRVGGNetCompact

MODELS_DIR = os.environ.get("REALESRGAN_MODELS", "/app/models")

# The compact v3 net is the fast one and the one Upscayl ships as
# "x4plus-v3"; the classic RRDB nets are the heavier original weights.
# name -> file, arch, scale, extra
REGISTRY = {
    "realesr-general-x4v3": ("realesr-general-x4v3.pth", "srvgg", 4, {"num_conv": 32}),
    "realesr-general-wdn-x4v3": ("realesr-general-wdn-x4v3.pth", "srvgg", 4,
                                 {"num_conv": 32}),
    "RealESRGAN_x4plus": ("RealESRGAN_x4plus.pth", "rrdb", 4, {"num_block": 23}),
    "RealESRGAN_x4plus_anime_6B": ("RealESRGAN_x4plus_anime_6B.pth", "rrdb", 4,
                                   {"num_block": 6}),
    "RealESRGAN_x2plus": ("RealESRGAN_x2plus.pth", "rrdb", 2, {"num_block": 23}),
}

# The names the GUI and the old ncnn models use.
ALIASES = {
    "upscayl-realesrgan-x4plus-v3": "realesr-general-x4v3",
    "upscayl-realesrgan-x4plus": "RealESRGAN_x4plus",
    "upscayl-realesrgan-x4plus-anime": "RealESRGAN_x4plus_anime_6B",
    "upscayl-realesrgan-x2plus": "RealESRGAN_x2plus",
    "realesr-general-wdn-x4v3": "realesr-general-wdn-x4v3",
}

DEFAULT_MODEL = "realesr-general-x4v3"


def cuda_available():
    """True when there is a card and PyTorch to drive it."""
    try:
        return torch.cuda.is_available()
    except Exception:
        return False


def list_models():
    out = []
    for name, (fname, _arch, _scale, _extra) in REGISTRY.items():
        if os.path.exists(os.path.join(MODELS_DIR, fname)):
            out.append(name)
    return out


def has_model(model):
    """True when there are torch weights for this model (or for the default)."""
    name = ALIASES.get(model, model) if model else DEFAULT_MODEL
    if name not in REGISTRY:
        name = DEFAULT_MODEL
    fname = REGISTRY[name][0]
    return os.path.exists(os.path.join(MODELS_DIR, fname))


def resolve(model):
    name = ALIASES.get(model, model)
    if name not in REGISTRY:
        name = DEFAULT_MODEL
    fname, arch, scale, extra = REGISTRY[name]
    return {"name": name, "file": os.path.join(MODELS_DIR, fname),
            "arch": arch, "scale": scale, "extra": extra}


def build_net(spec, device):
    if spec["arch"] == "srvgg":
        net = SRVGGNetCompact(num_feat=64, num_conv=spec["extra"].get("num_conv", 32),
                               upscale=spec["scale"], act_type="prelu")
    else:
        net = RRDBNet(num_in_ch=3, num_out_ch=3, scale=spec["scale"], num_feat=64,
                      num_grow_ch=32, **spec["extra"])
    raw = torch.load(spec["file"], map_location="cpu", weights_only=True)
    if isinstance(raw, dict):
        raw = raw.get("params_ema", raw.get("params", raw))
    clean = {}
    for k, v in raw.items():
        if k.startswith("params_ema."):
            k = k[len("params_ema."):]
        elif k.startswith("params."):
            k = k[len("params."):]
        clean[k] = v
    net.load_state_dict(clean, strict=True)
    return net.eval().to(device)


def tiled_forward(net, img, scale, tile=512, pad=10):
    """Run the net over the frame, in tiles when it is big.

    A whole 1080p frame at 4x does not fit in the activations of a
    64-channel net, so the frame is cut into tiles that do, each with a
    margin so the seams do not show.
    """
    if not tile or tile <= 0:
        return net(img)
    b, c, h, w = img.shape
    out = img.new_empty((b, c, h * scale, w * scale))
    for y0 in range(0, h, tile):
        for x0 in range(0, w, tile):
            y1 = min(y0 + tile, h)
            x1 = min(x0 + tile, w)
            ay0, ay1 = max(0, y0 - pad), min(h, y1 + pad)
            ax0, ax1 = max(0, x0 - pad), min(w, x1 + pad)
            res = net(img[:, :, ay0:ay1, ax0:ax1])
            ty0 = (y0 - ay0) * scale
            tx0 = (x0 - ax0) * scale
            out[:, :, y0 * scale:y1 * scale, x0 * scale:x1 * scale] = \
                res[:, :, ty0:ty0 + (y1 - y0) * scale, tx0:tx0 + (x1 - x0) * scale]
    return out


def _run(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _probe(video_path):
    probe = json.loads(_run(["ffprobe", "-v", "error", "-print_format", "json",
                             "-show_streams", "-show_format",
                             video_path]).stdout.decode())
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
    return int(v.get("width") or 0), int(v.get("height") or 0), (fps or 25.0)


def _read_exact(stream, n):
    """Read n bytes, or fewer if the stream ends."""
    chunks = []
    got = 0
    while got < n:
        part = stream.read(n - got)
        if not part:
            break
        chunks.append(part)
        got += len(part)
    return b"".join(chunks)


def upscale(video_path, out_path, model=DEFAULT_MODEL, scale=None, denoise=None,
            log=print, device=None):
    """Upscale a video on the card, streaming the frames through pipes."""
    device = device or ("cuda" if cuda_available() else "cpu")
    spec = resolve(model)
    model_scale = spec["scale"]
    want = float(scale or model_scale)
    pre = want / model_scale

    w, h, fps = _probe(video_path)
    if not w or not h:
        raise RuntimeError("that file has no video stream")
    dw = max(2, int(round(w * pre)) // 2 * 2)
    dh = max(2, int(round(h * pre)) // 2 * 2)
    ow, oh = dw * model_scale, dh * model_scale

    log(f"cuda upscale {w}x{h} -> {ow}x{oh} at {fps:.3f} fps, model {spec['name']}")

    net = build_net(spec, device)
    torch.backends.cudnn.benchmark = True
    if device == "cuda":
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True

    tile = int(os.environ.get("REALESRGAN_TILE", "512"))
    batch = int(os.environ.get("REALESRGAN_BATCH", "4"))

    # --- decode: the file becomes a stream of raw frames -------------------
    filters = []
    if denoise:
        filters.append(denoise)
    if pre != 1.0 or dw != w or dh != h:
        filters.append(f"scale={dw}:{dh}:flags=lanczos")
    dec = ["ffmpeg", "-v", "error", "-i", video_path]
    if filters:
        dec += ["-vf", ",".join(filters)]
    dec += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]

    # --- encode: the result goes straight back into a second pipe ---------
    enc = ["ffmpeg", "-v", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{ow}x{oh}",
           "-r", f"{fps:.6f}", "-i", "-",
           "-i", video_path, "-map", "0:v", "-map", "1:a?", "-c:a", "copy",
           "-c:v", "libx264", "-crf", "16", "-preset", "medium",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]

    decoder = subprocess.Popen(dec, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    encoder = subprocess.Popen(enc, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    frame_bytes = dw * dh * 3
    done = 0
    t0 = time.time()
    try:
        while True:
            buf = _read_exact(decoder.stdout, frame_bytes * batch)
            if not buf:
                break
            n = len(buf) // frame_bytes
            if n == 0:
                break
            arr = np.frombuffer(buf, dtype=np.uint8)[: n * frame_bytes]
            arr = arr.reshape(n, dh, dw, 3)
            x = torch.from_numpy(arr).to(device).permute(0, 3, 1, 2).float().div_(255.0)
            with torch.no_grad():
                y = tiled_forward(net, x, model_scale, tile=tile)
            y = y.clamp_(0, 1).mul_(255.0).round_().to(torch.uint8)
            y = y.permute(0, 2, 3, 1).contiguous().cpu().numpy()
            encoder.stdin.write(y.tobytes())
            done += n
            if done % (batch * 10) < batch:
                rate = done / max(0.001, time.time() - t0)
                log(f"model: {done} frames, {rate:.1f} fps")
    finally:
        try:
            encoder.stdin.close()
        except Exception:
            pass
        decoder.stdout.close()
        decoder.wait()
        err = encoder.stderr.read().decode("utf-8", "replace")
        encoder.wait()

    if encoder.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError("the encode failed:\n" + err[-600:])
    if not done:
        raise RuntimeError("no frames came out of that video")
    log(f"model finished: {done} frames in {time.time() - t0:.0f}s")
    return out_path
