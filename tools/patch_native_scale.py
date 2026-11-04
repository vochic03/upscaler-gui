#!/usr/bin/env python3
"""Patch engine.py: the AI must run at the model's native scale.

The NCNN Real-ESRGAN binary only tiles correctly at the scale the model was
trained at. Asked for any other -s it emulates that factor by tiling the
input, and the tiles do not line up, so the output comes back as a patchwork
of blocks at different magnifications. The engine used to ask for whatever
factor fitted the target, so a 4x model was often run at 2x or 3x.

The fix: always run the model at its native factor and pre-scale the input
so the result lands on the target. That is also less work for the GPU.

Also fixes the cloud path dropping the letterbox crop when the bilateral
de-noise was selected: the AI saw the uncropped video while the target
size was computed from the cropped one.
"""
import re
import sys

PATH = "/Users/vasilijemicic/upscaler-gui/engine.py"
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

# 1. the helper ------------------------------------------------------------
old = '''def pick_scale(width, height, target, allowed=(2, 3, 4)):'''
new = '''def native_scale_for(model):
    """The factor the model was trained at.

    The NCNN Real-ESRGAN binary only tiles correctly at the model's own
    scale: asked for another -s it emulates it by tiling the input, and the
    seams do not line up, so the output comes back as a patchwork of blocks
    at different magnifications.
    """
    for mid, (_label, _max_scale, _note, native) in MODELS.items():
        if mid == model:
            return native or 4
    return 4


def pick_scale(width, height, target, allowed=(2, 3, 4)):'''
sub(old, new, "native_scale_for helper")

# 2. the local path: native scale + pre-scale ------------------------------
old = '''        cap = opts.get("max_resolution") or ""
        target = target_size(info["width"], info["height"], cap) if cap else None
        if target:
            allowed = [s for s in (1, 2, 3, 4) if s <= max_scale_for(model)]
            scale = pick_scale(info["width"], info["height"], target, allowed)
            self.say(f"target size: {target[0]}x{target[1]} (inside {cap}); "
                     f"the AI runs at {scale}x and the result is resized to fit")
        # The size the AI itself produces. The blend needs both branches at
        # this size; the final resize to the target happens after it.
        ai_w = even(info["width"] * scale)
        ai_h = even(info["height"] * scale)
        if not target:
            self.say(f"target: {ai_w}x{ai_h} ({scale}x), model {model}, "
                     f"encoder {encoder}, look {look_name}")'''
new = '''        cap = opts.get("max_resolution") or ""
        target = target_size(info["width"], info["height"], cap) if cap else None
        # The model always runs at its own scale (see native_scale_for), and
        # the input is pre-scaled so the result lands on the target. That
        # keeps the tiles lined up and gives the GPU less to do.
        native = native_scale_for(model)
        scale = native
        if target:
            pre = min(target[0] / (info["width"] * native),
                      target[1] / (info["height"] * native))
            pre_w = even(max(2, round(info["width"] * pre)))
            pre_h = even(max(2, round(info["height"] * pre)))
            self.say(f"target size: {target[0]}x{target[1]} (inside {cap}); "
                     f"the AI runs at its native {native}x on a "
                     f"{pre_w}x{pre_h} frame, then the result is resized to fit")
        else:
            pre_w, pre_h = even(info["width"]), even(info["height"])
            self.say(f"target: {pre_w}x{pre_h} at the model's native {native}x, "
                     f"model {model}, encoder {encoder}, look {look_name}")
        # The size the AI itself produces. The blend needs both branches at
        # this size; the final resize to the target happens after it.
        ai_w = even(pre_w * native)
        ai_h = even(pre_h * native)'''
sub(old, new, "local path: native scale and pre-scale")

# 3. the local path: scale the frames that go to the AI ------------------
old = '''            if denoise and denoise_engine == "ffmpeg":
                # Denoise BEFORE the AI pass: an upscale amplifies mosquito
                # noise and MPEG blocking into "detail".
                extract += ["-vf", denoise]
            extract += ["-start_number", "1", "-f", "image2",'''
new = '''            vf = []
            if denoise and denoise_engine == "ffmpeg":
                # Denoise BEFORE the AI pass: an upscale amplifies mosquito
                # noise and MPEG blocking into "detail".
                vf.append(denoise)
            if (pre_w, pre_h) != (even(info["width"]), even(info["height"])):
                # Pre-scale, so the model's native factor lands on the target.
                vf.append(f"scale={pre_w}:{pre_h}:flags=lanczos")
            if vf:
                extract += ["-vf", ",".join(vf)]
            extract += ["-start_number", "1", "-f", "image2",'''
sub(old, new, "local path: pre-scale the extracted frames")

# 4. the cloud path: native scale + pre-scaled upload ---------------------
old = '''        cap = opts.get("max_resolution") or ""
        target = target_size(src_info["width"], src_info["height"], cap) if cap else None
        if target:
            self.say(f"target size: {target[0]}x{target[1]} (inside {cap})")
        motion = int(opts.get("motion") or 0)'''
new = '''        cap = opts.get("max_resolution") or ""
        target = target_size(src_info["width"], src_info["height"], cap) if cap else None
        if target:
            self.say(f"target size: {target[0]}x{target[1]} (inside {cap})")
        # Same rule as the local path: the model runs at its native scale and
        # the upload is pre-scaled so the result lands on the target.
        if engine != "dloral":
            scale = native_scale_for(model)
            if target:
                pre = min(target[0] / (src_info["width"] * scale),
                          target[1] / (src_info["height"] * scale))
                pre_w = even(max(2, round(src_info["width"] * pre)))
                pre_h = even(max(2, round(src_info["height"] * pre)))
                extra.append(f"scale={pre_w}:{pre_h}:flags=lanczos")
                self.say(f"the AI runs at its native {scale}x on a "
                         f"{pre_w}x{pre_h} frame")
        motion = int(opts.get("motion") or 0)'''
sub(old, new, "cloud path: native scale and pre-scaled upload")

# 5. the cloud path: the bilateral branch must keep the crop --------------
old = '''        if denoise_engine == "bilateral" and look["denoise"]:
            import lifecast_denoise
            self.say("de-noising with the Lifecast bilateral filter…")
            lifecast_denoise.denoise_video(
                upload_src, upload,
                radius=int(opts.get("bilateral_radius") or 7),
                strength=float(opts.get("bilateral_strength") or 0.85),
                log=lambda m: self.say(m))'''
new = '''        if denoise_engine == "bilateral" and look["denoise"]:
            import lifecast_denoise
            self.say("de-noising with the Lifecast bilateral filter…")
            # The crop and the de-interlace have to happen first. The
            # bilateral filter runs on a whole video, and if it ran on the
            # uncropped one the AI and the blend would disagree about the
            # framing - the output comes back with the bars still on it.
            staged = os.path.join(self.work, "staged.mp4")
            cloud.prepare_upload(upload_src, staged, denoise=None,
                                 extra_filters=extra)
            lifecast_denoise.denoise_video(
                staged, upload,
                radius=int(opts.get("bilateral_radius") or 7),
                strength=float(opts.get("bilateral_strength") or 0.85),
                log=lambda m: self.say(m))'''
sub(old, new, "cloud path: keep the crop in the bilateral branch")

if src == orig:
    print("nothing changed")
    sys.exit(1)
open(PATH, "w", encoding="utf-8").write(src)
print("engine.py patched")
