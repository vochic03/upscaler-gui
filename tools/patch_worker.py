#!/usr/bin/env python3
"""Patch worker/server.py.

1. The worker used whatever -s it was given. The NCNN Real-ESRGAN binary only
   tiles correctly at the scale its model was trained at; asked for another
   factor it tiles the input to emulate it and the seams do not line up, so
   the output comes back as a patchwork. The worker now clamps the factor to
   the model's own.

2. The DLoRAL path put the frames back into a video with the pattern "%s",
   which is not a valid image2 sequence, and it ignored that DLoRAL writes
   frame_0001.png upwards and drops the last frame. The frames are now
   renumbered into one contiguous sequence first.
"""
import os
import sys

PATH = "/Users/vasilijemicic/upscaler-gui/worker/server.py"
src = open(PATH, encoding="utf-8").read()
orig = src


def sub(old, new, label):
    global src
    if old not in src:
        print("MISS:", label)
        return False
    if src.count(old) != 1:
        print("AMBIGUOUS (%d):" % src.count(old), label)
        return False
    src = src.replace(old, new)
    print("ok:", label)
    return True


# 1. the native scales -----------------------------------------------------
old = '''# A model id -> the file name the binary wants.
MODELS_DIR = MODELS
'''
new = '''# A model id -> the file name the binary wants.
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
'''
sub(old, new, "native scale table")

# 2. use it in the AI pass -------------------------------------------------
old = '''    fps = fps or 25.0
    w = int(v.get("width") or 0) * scale // 2 * 2'''
new = '''    fps = fps or 25.0
    scale = scale_for(model, scale)
    w = int(v.get("width") or 0) * scale // 2 * 2'''
sub(old, new, "clamp the scale in upscale()")

# 3. the DLoRAL frame reassembly ------------------------------------------
old = '''    log(f"DLoRAL produced {len(pngs)} frames")
'''
new = '''    log(f"DLoRAL produced {len(pngs)} frames")

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
'''
sub(old, new, "renumber the DLoRAL frames")

old = '''    res = run(["ffmpeg", "-y", "-v", "error", "-framerate", f"{fps:.6f}",
               "-i", os.path.join(frames, "%s"), "-i", video_path,'''
new = '''    res = run(["ffmpeg", "-y", "-v", "error", "-framerate", f"{fps:.6f}",
               "-i", os.path.join(seq, "frame_%06d.png"), "-i", video_path,'''
sub(old, new, "fix the image2 pattern")

if src == orig:
    print("nothing changed")
    sys.exit(1)
open(PATH, "w", encoding="utf-8").write(src)
print("worker/server.py patched")
