#!/usr/bin/env python3
"""
Build a side-by-side run-off: the same clip through every option we have,
piled into one video you can full-screen and judge.

    python3 tools/collage.py <source.mp4> [seconds] [out.mp4] [--reuse]

Each panel is rendered to the same size and labelled, so the only thing that
differs is the treatment.

Labels are burned with PIL and composited with ffmpeg's `overlay` filter,
because the stock Homebrew ffmpeg has no libfreetype and therefore no
`drawtext`.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from engine import detect_crop  # noqa: E402  (same repo)

WORK = os.path.join(ROOT, "work", "collage")
API = "http://127.0.0.1:8790"

PANEL_W, PANEL_H = 640, 360

# The AI panels have the letterbox bars removed by the pipeline, so they come
# out at 1920x1014 (1.893:1). The two no-AI panels get the same crop applied,
# otherwise we would be comparing different framings - and the source panel
# would be a squashed 3:2 while the AI ones are 1.89:1.
ASPECT = 1920 / 1014

# (label, settings)
#
# The x4plus-v3 model is the one that reaches 1080p natively from a 540p
# source (4x = 2128x1200, then down to 1920x1080), so it is the fair base
# for comparing the treatments. The video-tuned x3 model is included because
# it looks different - and because it is the one that does not flicker.
#
# kind="scale"  -> plain ffmpeg resize, no AI at all
# kind="engine" -> the full pipeline through the GUI
VARIANTS = [
    ("1. Source - bars cropped only", {"kind": "scale", "flags": "bicubic"}),
    ("2. Lanczos - no AI at all", {"kind": "scale", "flags": "lanczos"}),
    ("3. Real-ESRGAN - faithful, sharp",
     {"kind": "engine", "model": "upscayl-realesrgan-x4plus-v3", "scale": 4,
      "look": "clean", "denoise_engine": "ffmpeg"}),
    ("4. Real-ESRGAN - polished, the DVD look",
     {"kind": "engine", "model": "upscayl-realesrgan-x4plus-v3", "scale": 4,
      "look": "polished", "denoise_engine": "bilateral", "blend_override": 0.35}),
    ("5. Real-ESRGAN - matte, flattest",
     {"kind": "engine", "model": "upscayl-realesrgan-x4plus-v3", "scale": 4,
      "look": "matte", "denoise_engine": "bilateral", "blend_override": 0.5}),
    ("6. Video-tuned model - polished",
     {"kind": "engine", "model": "xintao-realesr-animevideo-x3-v3", "scale": 3,
      "look": "polished", "denoise_engine": "bilateral", "blend_override": 0.35}),
]


def post(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))


def get(path):
    return json.load(urllib.request.urlopen(API + path, timeout=60))


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", path],
        stdout=subprocess.PIPE).stdout.decode()
    d = json.loads(out)
    v = next(s for s in d["streams"] if s.get("codec_type") == "video")
    return int(v["width"]), int(v["height"])


def plain_scale(src, dst, seconds, flags="lanczos", crop=None):
    """A plain resize - the no-AI baseline.

    The same letterbox crop the AI panels get, so all six panels show the
    same framing instead of one of them being a squashed 4:3.
    """
    pre = ""
    if crop:
        pre = f"crop={crop[0]}:{crop[1]}:{crop[2]}:{crop[3]},"
    vf = (f"{pre}scale={PANEL_W}:{PANEL_H}:"
          f"force_original_aspect_ratio=decrease,"
          f"pad={PANEL_W}:{PANEL_H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-t", str(seconds), "-i", src,
         "-vf", vf,
         "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p",
         "-r", "30", dst], check=True)
    return dst


def run_variant(src, dst, seconds, settings, crop=None):
    if settings.get("kind") == "scale":
        plain_scale(src, dst, seconds, settings.get("flags", "lanczos"), crop)
        print(f"    {os.path.basename(dst)}  (plain {settings.get('flags')})", flush=True)
        return dst

    body = {"path": src, "backend": "realesrgan", "encoder": "h265",
            "max_resolution": "1920x1080", "deinterlace": "auto", "crop": "auto",
            "motion": 0, "chunk_frames": 30}
    body.update({k: v for k, v in settings.items() if k != "kind"})
    r = post("/api/run", body)
    jid = r["id"]
    t0 = time.time()
    while True:
        time.sleep(3)
        j = get(f"/api/job?id={jid}")
        if j["state"] in ("done", "error", "cancelled"):
            break
        if time.time() - t0 > 7200:
            raise RuntimeError("timed out")
    if j["state"] != "done":
        raise RuntimeError(j.get("error") or j["state"])
    shutil.move(j["output"], dst)
    print(f"    {os.path.basename(dst)}  ({time.time()-t0:.0f}s)", flush=True)
    return dst


def font(size=24):
    for p in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
              "/System/Library/Fonts/Supplemental/Arial.ttf",
              "/System/Library/Fonts/Helvetica.ttc",
              "/Library/Fonts/Arial.ttf"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


def label_png(text, path):
    """A translucent black plate with white text, sized to the text."""
    f = font(24)
    tmp = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    d = ImageDraw.Draw(tmp)
    box = d.textbbox((0, 0), text, font=f)
    tw, th = box[2] - box[0], box[3] - box[1]
    pad = 10
    img = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=6,
                        fill=(0, 0, 0, 170))
    d.text((pad - box[0], pad - box[1]), text, font=f, fill=(255, 255, 255, 255))
    img.save(path)
    return path


def panel_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        stdout=subprocess.PIPE).stdout.decode().strip()
    return float(out)


def label_panel(path, label, dst, dur):
    """Burn one label onto one panel.

    The label PNG is fed as a looped input, so the output has to be bounded
    with -t: `-shortest` does not stop an infinite overlay source.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    lp = os.path.join(WORK, f"lbl_{stem}.png")
    label_png(label, lp)
    vf = (f"[0:v]scale={PANEL_W}:{PANEL_H}:force_original_aspect_ratio=decrease,"
          f"pad={PANEL_W}:{PANEL_H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[b];"
          f"[1:v]format=rgba[l];[b][l]overlay=10:10:format=auto[v]")
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", path,
           "-loop", "1", "-framerate", "30", "-i", lp,
           "-filter_complex", vf, "-map", "[v]", "-t", f"{dur:.3f}",
           "-c:v", "libx264", "-crf", "16", "-preset", "veryfast",
           "-pix_fmt", "yuv420p", "-r", "30", dst]
    subprocess.run(cmd, check=True)
    return dst


def build_collage(panels, out):
    """Pile the panels up and label them."""
    dur = min(panel_duration(p) for _label, p in panels)
    labelled = []
    for i, (label, path) in enumerate(panels):
        lp = os.path.join(WORK, f"labelled{i}.mp4")
        label_panel(path, label, lp, dur)
        print(f"    labelled {i + 1}/{len(panels)}", flush=True)
        labelled.append(lp)

    fc = []
    per_row = 3
    rows = []
    for r in range(0, len(labelled), per_row):
        idx = list(range(r, min(r + per_row, len(labelled))))
        fc.append("".join(f"[{i}:v]" for i in idx) + f"hstack=inputs={len(idx)}[r{r}]")
        rows.append(f"[r{r}]")
    fc.append("".join(rows) + f"vstack=inputs={len(rows)}[out]")
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for p in labelled:
        cmd += ["-i", p]
    cmd += ["-filter_complex", ";".join(fc), "-map", "[out]", "-t", f"{dur:.3f}",
            "-c:v", "libx264", "-crf", "17", "-preset", "medium",
            "-pix_fmt", "yuv420p", "-r", "30", "-movflags", "+faststart", out]
    subprocess.run(cmd, check=True)
    return out


def main():
    args = [a for a in sys.argv[1:] if a != "--reuse"]
    reuse = "--reuse" in sys.argv[1:]
    start = 0.0
    if "--start" in args:
        i = args.index("--start")
        start = float(args[i + 1])
        del args[i:i + 2]
    src = args[0]
    seconds = float(args[1]) if len(args) > 1 else 12.0
    out = args[2] if len(args) > 2 else os.path.join(WORK, "runoff.mp4")
    if not reuse:
        shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)

    # A cut of the source, so every variant works on identical frames.
    clip = os.path.join(WORK, "clip.mp4")
    if reuse and os.path.exists(clip):
        print(f"clip: {probe(clip)}  (reused)", flush=True)
    else:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(start),
                        "-t", str(seconds), "-i", src,
                        "-an", "-c:v", "libx264", "-crf", "14", "-preset", "veryfast",
                        "-pix_fmt", "yuv420p", clip], check=True)
        print(f"clip: {probe(clip)}  {seconds}s from {start}s", flush=True)

    crop = detect_crop(clip)
    print(f"letterbox crop: {crop}", flush=True)

    panels = []
    for i, (label, settings) in enumerate(VARIANTS):
        dst = os.path.join(WORK, f"panel{i}.mp4")
        print(f"[{i+1}/{len(VARIANTS)}] {label}", flush=True)
        if reuse and os.path.exists(dst) and os.path.getsize(dst) > 1000:
            print(f"    {os.path.basename(dst)}  (reused)", flush=True)
            panels.append((label, dst))
            continue
        try:
            run_variant(clip, dst, seconds, settings, crop)
            panels.append((label, dst))
        except Exception as e:
            print(f"    FAILED: {e}", flush=True)
    if not panels:
        raise SystemExit("nothing worked")
    build_collage(panels, out)
    print("collage:", out, flush=True)


if __name__ == "__main__":
    main()
