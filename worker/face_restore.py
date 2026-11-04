#!/usr/bin/env python3
"""Put the faces back.

Real-ESRGAN enlarges the picture. It does not know what a face is, so the
face stays as soft as everything else - and in a close-up the face is the
only thing in the frame.

**RestoreFormer** (TencentARC/GFPGAN, released with v1.3.4) is a masked
transformer trained to repair faces. It was chosen over GFPGAN v1.4 because
it keeps more of the original: it repairs a face instead of replacing it,
which is what a film-like look needs. A beauty filter would fight the
"polished DVD" treatment.

Per frame: find the faces (RetinaFace), line them up on the five points,
run the transformer on the 512x512 crop, paste it back. The frames stream
through pipes, so a film never lands on the disk as pictures.

The detector runs on the CPU (onnxruntime) and the transformer on the card.
"""
import os
import subprocess
import sys
import time

import numpy as np
import torch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_MODEL = os.environ.get(
    "FACE_MODEL", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "models", "RestoreFormer.pth"))


def _read_exact(stream, n):
    """Read exactly n bytes, or less at the end of the stream."""
    out = b""
    while len(out) < n:
        chunk = stream.read(n - len(out))
        if not chunk:
            break
        out += chunk
    return out


def _probe(video_path):
    """width, height, fps of the video."""
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,avg_frame_rate", "-of", "default=nw=1", video_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    w = h = 0
    fps = 25.0
    for line in res.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("width="):
            w = int(line.split("=")[1])
        elif line.startswith("height="):
            h = int(line.split("=")[1])
        elif line.startswith("avg_frame_rate="):
            try:
                num, den = line.split("=")[1].split("/")
                fps = float(num) / float(den) if float(den) else 25.0
            except Exception:
                fps = 25.0
    return w, h, fps


class FaceRestorer:
    """The model, the detector and the glue between them."""

    def __init__(self, model_path=DEFAULT_MODEL, device=None, face_size=512,
                 min_size=None, log=print):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.log = log
        self.face_size = face_size
        # A face smaller than this is left alone: the model would invent a
        # sharp face out of a blur, and the invention flickers from frame to
        # frame.
        self.min_size = int(os.environ.get("FACE_MIN_SIZE", "48")) if min_size is None else min_size

        from restoreformer_arch import RestoreFormer

        self.net = RestoreFormer()
        ckpt = torch.load(model_path, map_location="cpu", weights_only=False)
        state = ckpt.get("params_ema", ckpt.get("params", ckpt))
        missing, unexpected = self.net.load_state_dict(state, strict=False)
        missing = [k for k in missing if not k.startswith("encoder.")] + \
                  [k for k in missing if k.startswith("encoder.")]
        self.net.eval().to(self.device)

        from facexlib.utils.face_restoration_helper import FaceRestoreHelper

        self.helper = FaceRestoreHelper(
            upscale_factor=1,
            face_size=face_size,
            det_model="retinaface_resnet50",
            use_parse=False,
            device=torch.device(self.device),
            model_rootpath=os.environ.get("FACEXLIB_WEIGHTS") or None,
        )
        self.log(f"the face model is ready ({device if device else self.device})")

    def restore(self, frame_rgb):
        """One frame in, one frame out. Same size."""
        import cv2

        bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
        h = self.helper
        h.clean_all()
        h.read_image(bgr)
        h.get_face_landmarks_5()
        if not h.det_faces:
            return frame_rgb
        h.align_warp_face()
        if not h.cropped_faces:
            return frame_rgb
        # The inverse matrices are what puts the face back where it was.
        h.get_inverse_affine()

        faces = h.cropped_faces
        dets = h.det_faces
        batch = []
        keep = []
        for idx, (det, crop) in enumerate(zip(dets, faces)):
            # det is [x0, y0, x1, y1, score] in the frame's own pixels.
            bbox = det[0:4] if det is not None else None
            size = 0
            if bbox is not None:
                size = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
            if size and size < self.min_size:
                # too small to repair: keep the original crop
                h.add_restored_face(crop)
                continue
            # The crop comes out of facexlib in BGR (it works in OpenCV's
            # order); the model was trained on RGB. Getting this backwards
            # swaps red and blue in the restored face, and a face with the
            # wrong colour in it is worse than a soft one.
            x = torch.from_numpy(crop[:, :, ::-1].copy()).to(self.device)
            x = x.permute(2, 0, 1).float().div(255.0).sub(0.5).div(0.5)
            x = x.unsqueeze(0)
            batch.append(x)
            keep.append(idx)

        if batch:
            x = torch.cat(batch, dim=0)
            with torch.no_grad():
                out = self.net(x)
            if isinstance(out, (tuple, list)):
                out = out[0]
            out = out.clamp(-1, 1).add(1).div(2).mul(255).round().to(torch.uint8)
            out = out.permute(0, 2, 3, 1).cpu().numpy()
            for pos, idx in enumerate(keep):
                h.add_restored_face(out[pos][:, :, ::-1].copy())  # RGB -> BGR

        result = h.paste_faces_to_input_image()
        if result is None:
            return frame_rgb
        return cv2.cvtColor(result, cv2.COLOR_BGR2RGB)


def restore_video(video_path, out_path, model_path=DEFAULT_MODEL, device=None,
                  log=print, face_size=512):
    """Restore the faces in a whole video, streaming the frames."""
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    w, h, fps = _probe(video_path)
    if not w or not h:
        raise RuntimeError("that file has no video stream")
    log(f"face restore {w}x{h} at {fps:.3f} fps on {device}")

    restorer = FaceRestorer(model_path=model_path, device=device,
                             face_size=face_size, log=log)

    dec = ["ffmpeg", "-v", "error", "-i", video_path,
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    enc = ["ffmpeg", "-v", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
           "-r", f"{fps:.6f}", "-i", "-",
           "-i", video_path, "-map", "0:v", "-map", "1:a?", "-c:a", "copy",
           "-c:v", "libx264", "-crf", "16", "-preset", "medium",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]

    decoder = subprocess.Popen(dec, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    encoder = subprocess.Popen(enc, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    frame_bytes = w * h * 3
    done = 0
    faces = 0
    t0 = time.time()
    try:
        while True:
            buf = _read_exact(decoder.stdout, frame_bytes)
            if not buf:
                break
            if len(buf) < frame_bytes:
                break
            arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3)
            out = restorer.restore(arr)
            encoder.stdin.write(np.ascontiguousarray(out).tobytes())
            done += 1
            if done % 50 == 0:
                rate = done / max(0.001, time.time() - t0)
                log(f"faces: {done} frames, {rate:.1f} fps")
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
    log(f"face restore finished: {done} frames in {time.time() - t0:.0f}s")
    return out_path


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("out")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--device", default=None)
    args = ap.parse_args()
    restore_video(args.video, args.out, model_path=args.model, device=args.device)
