#!/usr/bin/env python3
"""
The de-noise step from Lifecast's UpscaleVideo.ai.

Their app (github.com/fbriggs/lifecast_public, lifecast_apps/source/
upscale_video.cc) does this before the AI pass:

    const float sigma = bilateral_filter_radius * 0.6;
    const float sigma_color = 0.1;
    cv::Mat bilateral = opencv::bilateralDenoise(src, radius, sigma, sigma_color);
    prefiltered = bilateral * strength + src * (1 - strength);

and their defaults are radius 7, strength 0.85.

Two things worth noticing, because they are what makes the look work:

  * the denoised frame is **mixed back** with the original by `strength`, not
    used on its own. At 0.85 you keep a sixth of the original detail;
  * it runs on the float image (CV_32FC3, 0..1), not on 8-bit, which is why
    the mixing does not band.

This is the same OpenCV bilateral filter, so it can be run here with the
same numbers. ffmpeg has no bilateral filter of its own (the ffmprovisr
build adds one), so this is done with OpenCV on the extracted frames.

It is a per-frame cost of roughly 30-80 ms at 1080p, which is nothing
compared with the AI pass that follows it.
"""

import json
import os
import sys

try:
    import cv2
    import numpy as np
except ImportError as e:  # pragma: no cover
    raise SystemExit(f"OpenCV is needed for the Lifecast de-noise: {e}")


def bilateral_params(radius=7, strength=0.85):
    """Their parameters, spelled out."""
    return int(radius), float(radius) * 0.6, 0.1, float(strength)


def denoise_image(img, radius=7, strength=0.85):
    """One frame, in 8-bit BGR, as 8-bit BGR back."""
    r, sigma, sigma_color, s = bilateral_params(radius, strength)
    src = img.astype(np.float32) / 255.0
    if hasattr(cv2, "bilateralDenoise"):
        filtered = cv2.bilateralDenoise(src, r, sigma, sigma_color)
    else:
        filtered = cv2.bilateralFilter(src, r, sigma_color, sigma)
    mixed = filtered * s + src * (1.0 - s)
    return np.clip(mixed * 255.0, 0, 255).astype(np.uint8)


def process_dir(frames_dir, radius=7, strength=0.85, log=print):
    """De-noise every PNG in a directory, in place."""
    names = sorted(f for f in os.listdir(frames_dir) if f.lower().endswith(".png"))
    if not names:
        return 0
    for i, name in enumerate(names):
        path = os.path.join(frames_dir, name)
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            continue
        out = denoise_image(img, radius=radius, strength=strength)
        cv2.imwrite(path, out)
        if (i + 1) % 25 == 0:
            log(f"de-noised {i + 1}/{len(names)} frames")
    return len(names)


def denoise_video(src, dst, radius=7, strength=0.85, log=print):
    """De-noise a whole video, for the path that hands a finished video on.

    The per-chunk path inside the pipeline is cheaper, because it works on
    the frames it has already extracted. This one is for the cloud path,
    where the video goes up as a file.
    """
    import subprocess
    import tempfile
    import shutil

    # The frame rate has to come from the file: hardcoding 25 would quietly
    # slow down a 50 fps source.
    fps = 25.0
    try:
        info = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", src],
            stdout=subprocess.PIPE).stdout.decode())
        v = next(s for s in info["streams"] if s.get("codec_type") == "video")
        num, den = (v.get("avg_frame_rate") or "25/1").split("/")
        if float(den):
            fps = float(num) / float(den)
    except Exception:
        pass

    work = tempfile.mkdtemp(prefix="bilateral-")
    frames = os.path.join(work, "frames")
    os.makedirs(frames)
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src,
                        "-start_number", "1", "-f", "image2",
                        os.path.join(frames, "%06d.png")], check=True)
        process_dir(frames, radius=radius, strength=strength, log=log)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-framerate", f"{fps:.6f}",
                        "-i", os.path.join(frames, "%06d.png"), "-i", src,
                        "-map", "0:v", "-map", "1:a?", "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
                        "-r", f"{fps:.6f}", "-c:a", "copy", dst], check=True)
        return dst
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: lifecast_denoise.py <frames_dir> [radius] [strength]")
    n = process_dir(sys.argv[1],
                    int(sys.argv[2]) if len(sys.argv) > 2 else 7,
                    float(sys.argv[3]) if len(sys.argv) > 3 else 0.85)
    print(f"de-noised {n} frames")
