#!/usr/bin/env python3
"""
Upscale engine for Holloway's Upscaler (Real-ESRGAN NCNN).

The upstream project ships a shell script that:
  * extracts every frame of the video with ffmpeg,
  * runs the AI binary once PER FRAME,
  * reassembles the frames with ffmpeg.

On this machine (Apple M1 Pro, native arm64 build, Vulkan through the
system's MoltenVK) one frame costs 1-43 s depending on the model, and
launching the binary per frame adds the model load to every single frame.
Two changes make it usable:

  * the binary accepts a *directory* and processes it in one process, so the
    model is loaded once per chunk instead of once per frame;
  * the frames are pushed through a pipe into one long-lived ffmpeg, so no
    huge pile of 4K PNGs has to sit on the disk.

The video is also normalised to constant frame rate / constant bit rate
first: the upstream script only works with CBR video, and a variable frame
rate source would otherwise produce a broken output.
"""

import ffbin
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "vendor", "upscaler")
BINARY = os.path.join(VENDOR, "bin", "mac-amd64")
MODELS_DIR = os.path.join(VENDOR, "models")

# Model id -> (label, max scale, note, native scale).
#
# The scale limits come from the upstream model list. The "native scale" is
# the factor the network was trained for: asking for less makes the tool run
# the network at its own factor and then downscale, which costs the same
# time and gains nothing.
MODELS = {
    "xintao-realesr-animevideo-x2-v3": ("Realesr-animevideo v3 (x2, video-tuned)", 2,
                                         "Trained on video: the most temporally stable, least flicker", 2),
    "xintao-realesr-animevideo-x3-v3": ("Realesr-animevideo v3 (x3, video-tuned)", 3,
                                         "Video-tuned, 3x", 3),
    "xintao-realesr-animevideo-x4-v3": ("Realesr-animevideo v3 (x4, video-tuned)", 4,
                                         "Video-tuned, 4x", 4),
    "upscayl-realesrgan-x4plus-v3": ("Upscayl RealESRGAN x4plus v3", 4,
                                      "Upscayl's tuned RealESRGAN: general purpose, fast, strong detail", 4),
    "upscayl-ultrasharp": ("Upscayl Ultrasharp", 4,
                            "General purpose. A very deep network - about 15x the work of the video models", 4),
    "upscayl-ultrasharp-v2": ("Upscayl Ultrasharp v2", 4,
                               "General purpose, newer", 4),
    "upscayl-realesrgan-x4plus": ("Upscayl RealESRGAN x4plus", 4,
                                   "General purpose, deep network", 4),
    "upscayl-realesrgan-x4plus-anime": ("Upscayl RealESRGAN x4plus-anime", 4,
                                         "For animation, not for broadcast footage", 4),
    "upscayl-remacri": ("Upscayl Remacri", 4,
                         "Gentler, better at keeping fine texture. Very deep network", 4),
    "upscayl-ultramix_balanced": ("Upscayl Ultramix balanced", 4,
                                   "Balanced general model. Very deep network", 4),
    "xintao-realesrgan-x4plus": ("RealESRGAN x4plus (general)", 4,
                                  "The classic general model. Sharpest on stills, flickers on video", 4),
    "xintao-realesrgan-x4plus-anime": ("RealESRGAN x4plus-anime", 4,
                                        "For animation, not for broadcast footage", 4),
}

# Output encoders offered in the UI.
ENCODERS = {
    "h264": ("H.264 (mp4, plays everywhere)", ["-c:v", "libx264", "-crf", "17", "-preset", "slow",
                                                 "-pix_fmt", "yuv420p", "-profile:v", "high"]),
    "h265": ("HEVC / H.265 (mp4, smaller, Apple friendly)", ["-c:v", "libx265", "-crf", "18", "-preset", "slow",
                                                              "-pix_fmt", "yuv420p", "-tag:v", "hvc1"]),
    "h265-10": ("HEVC 10-bit (mp4, keeps broadcast gradients smooth, twice the size)",
                ["-c:v", "libx265", "-crf", "20", "-preset", "slow",
                 "-pix_fmt", "p010le", "-tag:v", "hvc1"]),
    "prores": ("ProRes 422 (mov, for editing)", ["-c:v", "prores_ks", "-profile:v", "3",
                                                  "-pix_fmt", "yuv422p10le"]),
    "vp9": ("VP9 (webm)", ["-c:v", "libvpx-vp9", "-crf", "20", "-b:v", "0", "-row-mt", "1"]),
}


# The "looks". These are the treatments, not the upscalers.
#
# "polished" is the look from the Topaz thread about a DVD-like image:
# QTGMC deinterlace, two upscalers blended at 65%, *negative* sharpening
# ("Improve details and sharpening to about -30 - I prefer softer output"),
# and a little noise added back to hide the washed-out patches that MPEG
# compression leaves behind.
#
# The important part is that a soft look is made by taking detail AWAY:
# blending the AI result with a plain lanczos enlargement at 65/35, then
# blurring a touch and adding grain. An AI upscale on its own looks waxy
# and over-sharp; this is what stops it.
LOOKS = {
    # "Enhance" is the one that sharpens. The other looks are built for a
    # film look - they blend in a plain enlargement and soften it, which
    # is the opposite of what you want when the source is a DVD capture:
    # the compression mush goes into the diffusion, the diffusion
    # faithfully reproduces the mush, and the result is soft in a way
    # nothing can undo afterwards.
    #
    # This one denoises first (the mush goes before the diffusion, not
    # after it), keeps the diffusion's invented detail (only a little of
    # the plain enlargement is blended in, for stable edges), sharpens
    # with CAS, and adds the grain and the glow that give the picture
    # its depth.
    "enhance": {
        "label": "Enhance — denoised, crisp, with depth",
        "denoise": "hqdn3d=3:2:4.5:3",
        "blend": 0.18,
        "soften": 0.0,
        "cas": 0.2,
        "grain": 4,
        "grain_mode": "perlin",
        "grain_tscale": 0.08,
        "halation": 0.05,
        # The grade is a curve on the luma alone (see grade_filters): a
        # touch of gamma to give the midtones back, a toe that keeps the
        # shadows off the floor, and the white pinned where it is. It used
        # to be eq=contrast=1.02, which pivots on mid-grey: on this dark
        # material (median 41 of 255) that took the median down six levels
        # and closed the hair and the faces up into black.
        "gamma": 1.04,
        "saturation": 0.04,
        "black_lift": 0.01,
    },
    "clean": {
        "label": "Clean — faithful enlargement, no treatment",
        "denoise": "",
        "blend": 0.0,      # 0 = pure AI, 1 = pure lanczos
        "soften": 0.0,     # negative sharpening, in unsharp units
        "grain": 0,
        "contrast": 0.0,
        "saturation": 0.0,
    },
    "polished": {
        "label": "Polished — soft, film-like, the 'DVD' look",
        "denoise": "hqdn3d=3.5:2.5:6:4",
        "blend": 0.35,
        # Less blur than before: cas now brings the edges back without
        # ringing them, which is what unsharp cannot do.
        "soften": -0.28,
        "cas": 0.14,
        "grain": 5,
        "grain_mode": "perlin",
        "grain_tscale": 0.08,
        "halation": 0.07,
        "gamma": 1.04,
        "saturation": 0.05,
        "chroma_soft": True,     # coarser, softer chroma
        "black_lift": 0.012,     # the shadows never reach zero
    },
    "matte": {
        "label": "Matte — flatter and softer still, almost no sharpening",
        "denoise": "hqdn3d=5:4:8:6",
        "blend": 0.5,
        "soften": -0.5,
        "cas": 0.08,
        "grain": 8,
        "grain_mode": "perlin",
        "grain_tscale": 0.08,
        "halation": 0.07,
        "gamma": 1.04,
        "saturation": 0.03,
        "chroma_soft": True,     # coarser, softer chroma
        "black_lift": 0.016,     # the shadows never reach zero
    },
    "clean8": {
        "label": "Clean + denoise only — no grain, no grade",
        "denoise": "hqdn3d=3:2:4.5:3",
        "blend": 0.0,
        "soften": 0.0,
        "grain": 0,
        "contrast": 0.0,
        "saturation": 0.0,
    },
}


# The presets. This is the whole point of the tool: one choice, not twenty.
#
# Each preset is a complete answer to "what do you want done to this
# video", and it fills in everything else. The individual settings still
# exist underneath, in the Advanced panel, for the times you want to
# override one of them.
PRESETS = [
    {
        "id": "enhance",
        "label": "Enhance — SeedVR2 on a rented GPU: detail and depth",
        "note": "SeedVR2 is a video diffusion model: it runs on the full "
                "1080p and invents the detail the capture lost, frame by "
                "frame, so it stays put instead of shimmering. The source "
                "is de-noised first (the compression goes before the "
                "diffusion, not after it), the result is sharpened with "
                "CAS, and a little grain and glow give it depth. A "
                "72-second 1080p film is about 1.7 hours and $1.60 on a "
                "card, and the result is 1080p - the same size as the "
                "source, with the detail put back.",
        "backend": "cloud",
        "cloud_engine": "seedvr2",
        "rent": True,
        "model": "",
        "scale": 1,
        "max_scale": 1,
        "look": "enhance",
        "denoise_engine": "ffmpeg",
        "encoder": "h264",
    },
    {
        "id": "faithful",
        "label": "Faithful — enlarge it, change nothing else",
        "note": "Real-ESRGAN on this Mac. Sharp and honest: it enlarges what "
                "is actually in the picture.",
        "backend": "realesrgan",
        "model": "xintao-realesr-animevideo-x2-v3",
        "scale": 2,
        "max_scale": 2,
        "look": "clean",
        "denoise_engine": ffbin.ffmpeg(),
        "encoder": "h264",
    },
    {
        "id": "polished",
        "label": "Polished — soft, film-like, the DVD look",
        "note": "Real-ESRGAN, de-noised with Lifecast's bilateral filter, then "
                "blended 65/35 with a plain enlargement, softened, and given "
                "static grain. The Topaz recipe.",
        "backend": "realesrgan",
        "model": "xintao-realesr-animevideo-x2-v3",
        "scale": 2,
        "max_scale": 2,
        "look": "polished",
        "denoise_engine": "bilateral",
        "encoder": "h264",
    },
    {
        "id": "cloud",
        "label": "Polished, on a rented GPU — your Mac stays cool",
        "note": "The same as Polished, but a card is hired for the job and "
                "given back when it ends. Costs cents, and your Mac only "
                "does the light CPU work.",
        "backend": "cloud",
        "cloud_engine": "realesrgan",
        "rent": True,
        "model": "xintao-realesr-animevideo-x2-v3",
        "scale": 2,
        "max_scale": 2,
        "look": "polished",
        "denoise_engine": "bilateral",
        "encoder": "h264",
    },
    {
        "id": "regenerate",
        "label": "Regenerate — one-step diffusion, on a rented GPU",
        "note": "DLoRAL: one step of diffusion, temporally consistent, 4x. "
                "It invents detail, but consistently across the frames. It "
                "needs CUDA, so it has to be rented - the card is hired for "
                "the job and returned afterwards. It works from a canvas a "
                "quarter of the size of the result, so a 1080p film goes "
                "up as 480x270 and comes back at 1080p. The default image "
                "has it in it (bzz1/upscaler:both), and it needs a card "
                "with 48 GB of VRAM or more at 1080p.",
        "backend": "cloud",
        "cloud_engine": "dloral",
        "rent": True,
        "model": "",
        "scale": 4,
        "max_scale": 4,
        "look": "polished",
        "denoise_engine": "bilateral",
        "encoder": "h264",
    },
    {
        "id": "best",
        "label": "Go all in — the strongest model, on a rented GPU",
        "note": "The deep RealESRGAN model that costs 57 hours on this Mac, "
                "but about two euros on a rented card. The most detail it can "
                "give you.",
        "backend": "cloud",
        "cloud_engine": "realesrgan",
        "rent": True,
        "model": "xintao-realesrgan-x4plus",
        "scale": 4,
        "max_scale": 4,
        "look": "polished",
        "denoise_engine": "bilateral",
        "encoder": "h265",
    },
    {
        "id": "star",
        "label": "STAR — diffusion with a text prompt (cloud)",
        "note": "The steerable one: it rebuilds the missing detail from a "
                "prompt, so the direction is yours to write. It is a "
                "generation rather than an enlargement, and it works on a "
                "SHORT CLIP - a couple of seconds, shrunk to what the "
                "model can hold - so it is a tool for a shot, not for a "
                "film. It wants a big card (48 GB of VRAM or more) and it "
                "is the most expensive of the four.",
        "backend": "cloud",
        "cloud_engine": "star",
        "rent": True,
        "prompt": "1990s DVD player, soft analog bloom, gentle film grain, "
                  "slightly faded colour",
        "model": "",
        "scale": 4,
        "max_scale": 4,
        "look": "polished",
        "encoder": "h264",
    },
]


def detect_crop(path, seconds=2.0, limit=24):
    """Find the letterbox bars, so the black is not upscaled too.

    Old TV transfers and music videos are often pillarboxed inside a 4:3
    frame. Upscaling the bars is a waste of pixels and of your time.
    """
    cmd = [ffbin.ffmpeg(), "-v", "info", "-ss", "3", "-i", path, "-t", str(seconds),
           "-vf", f"cropdetect={limit}:2:0", "-frames:v", "80", "-f", "null", "-"]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    found = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", res.stdout.decode("utf-8", "replace"))
    if not found:
        return None
    # The detector reports a value for every frame and it moves as the shot
    # changes, so take the most common one rather than the last.
    counts = {}
    for v in found:
        counts[v] = counts.get(v, 0) + 1
    w, h, x, y = max(counts.items(), key=lambda kv: kv[1])[0]
    w, h, x, y = int(w), int(h), int(x), int(y)
    if w <= 0 or h <= 0:
        return None
    return (w, h, x, y)


def target_size(width, height, cap):
    """The largest size with this aspect ratio that fits inside `cap`.

    `cap` is "1920x1080" or None. Returns (w, h), both even.
    """
    if not cap:
        return None
    try:
        cw, ch = (int(v) for v in str(cap).lower().split("x"))
    except Exception:
        return None
    if cw <= 0 or ch <= 0:
        return None
    scale = min(cw / width, ch / height)
    w = even(int(round(width * scale)))
    h = even(int(round(height * scale)))
    return (max(2, w), max(2, h))


def _apply_motion(src, dst, factor, log=print):
    """Interpolate the frames so the motion is smoother.

    RIFE invents the frames between the ones you have, so 25 fps PAL
    footage becomes 50 fps and the judder goes away. It is the same
    ncnn-vulkan shape as the upscaler, so it runs on the Apple GPU the
    same way.

    It runs on the source, before the upscale: that keeps the pipeline
    streaming (no pile of 4K frames on disk) and it is what the in-browser
    tools do too.
    """
    import rife
    if not rife.available():
        log("RIFE is not installed here, skipping the motion smoothing")
        return src
    log(f"smoothing the motion: {factor}x frame interpolation")
    rife.interpolate(src, dst, factor=factor, model="rife-v4.6", log=log)
    return dst


def max_scale_for(model):
    """The biggest factor this model can do."""
    for mid, (_label, max_scale, _note, _native) in MODELS.items():
        if mid == model:
            return max_scale or 4
    return 4


def native_scale_for(model):
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


def pick_scale(width, height, target, allowed=(2, 3, 4)):
    """Which factor to run the AI at, given the size we want to end up with.

    The smallest factor whose output already covers the target is best: the
    final resize is then a downscale, which throws away nothing. If no factor
    covers it, use the biggest one and let the final resize add the rest.
    """
    tw, th = target
    options = sorted({s for s in allowed if s} | {1})
    for s in options:
        if width * s >= tw and height * s >= th:
            return s
    return max(options)


def ffprobe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode("utf-8", "replace")
    return json.loads(out)


def _last_video_pts(path):
    """Timestamp of the last video packet.

    A file recorded straight to WebM (MediaRecorder) often carries no
    duration at all, so the duration has to come from the packets.
    """
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "packet=pts_time", "-of", "csv=p=0", path],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode("utf-8", "replace")
    last = 0.0
    for line in out.splitlines():
        line = line.strip().rstrip(",")
        if not line or line == "N/A":
            continue
        try:
            last = max(last, float(line))
        except ValueError:
            continue
    return last


def _count_packets(path):
    """Number of video packets. Cheap: no decoding."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
         "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", path],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode("utf-8", "replace")
    for line in out.splitlines():
        line = line.strip().rstrip(",")
        if line.isdigit():
            return int(line)
    return 0


def video_info(path):
    """Summarise a file: size, duration, fps, resolution, codecs."""
    try:
        info = ffprobe(path)
    except Exception as e:
        raise ValueError(f"ffprobe could not read this file: {e}")
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        raise ValueError("no video stream in that file")
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)

    def rate(value):
        try:
            num, den = (value or "0/0").split("/")
            if float(den):
                return float(num) / float(den)
        except Exception:
            pass
        return 0.0

    # The average rate is the honest one for a variable frame rate file;
    # r_frame_rate is only the container's declared base rate.
    fps = rate(v.get("avg_frame_rate")) or rate(v.get("r_frame_rate")) or 25.0
    duration = float(info.get("format", {}).get("duration") or 0)
    if not duration and v.get("duration"):
        duration = float(v["duration"])
    if not duration:
        # No duration in the container: take it from the last packet.
        duration = _last_video_pts(path)
    frames = _count_packets(path)
    if duration and fps and not frames:
        frames = int(round(duration * fps))
    return {
        "path": path,
        "name": os.path.basename(path),
        "size": int(info.get("format", {}).get("size") or os.path.getsize(path)),
        "duration": duration,
        "frames": frames,
        "fps": fps,
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "vcodec": v.get("codec_name") or "?",
        "acodec": (a or {}).get("codec_name"),
        "pix_fmt": v.get("pix_fmt") or "",
        # The de-interlace "auto" mode reads this. It was never filled in,
        # so "auto" could never fire and an interlaced DVD capture went to
        # the AI with its combing in it, unless "on" was chosen by hand.
        "field_order": v.get("field_order") or "",
        "bit_rate": int(info.get("format", {}).get("bit_rate") or 0),
    }


def even(n):
    return max(2, int(n) // 2 * 2)


def grade_filters(look):
    """The grade of a look, as ffmpeg filters that stay in YUV.

    The tone curve works on the luma alone, in limited range: with x the
    luma between black (16) and white (235) as 0..1,

        out = lift + (1 - lift) * x ** (1 / gamma)

    so the white stays where it is, the black is raised to `lift` (the
    shadows of an analogue transfer never reach zero), and a gamma above 1
    opens the midtones and the shadows without moving either end.

    What this replaces, and why, measured on 100 frames of the SeedVR2
    output (Y code values; the source is untagged, i.e. limited range):

      * eq=contrast=1.02 pivots on 128. The picture is dark - median 41 -
        so everything that matters went DOWN: the median by 3 levels on
        its own, and with the rest of the chain by 6 (41 -> 35).
      * colorlevels=rimin=0.01 was meant as a black lift, but rimin is the
        INPUT black point: it pulls the shadows down (p5 18 -> 16), not up.
        It also only works in RGB, so ffmpeg converted every frame to RGB
        and back, which clips the saturated stage lights (p95 158 -> 155).

    Saturation is applied to the chroma planes only (eq with contrast 1
    leaves the luma alone). A look can still name "contrast"; it is
    honoured, but no built-in look uses it any more.
    """
    out = []
    gamma = float(look.get("gamma") or 1.0)
    lift = float(look.get("black_lift") or 0)
    if gamma != 1.0 or lift:
        out.append("lutyuv=y='16+219*(%s+(1-%s)*pow(clip((val-16)/219\\,0\\,1)\\,1/%s))'"
                   % (repr(lift), repr(lift), repr(gamma)))
    contrast = float(look.get("contrast") or 0)
    saturation = float(look.get("saturation") or 0)
    if contrast or saturation:
        out.append(f"eq=contrast={1 + contrast}:saturation={1 + saturation}")
    return out


class Job:
    """One upscale run."""

    def __init__(self, job_id, src, out_path, opts, workdir):
        self.id = job_id
        self.src = src
        self.out = out_path
        self.opts = opts
        self.work = workdir
        self.state = "queued"
        self.stage = ""
        self.progress = 0.0
        self.message = ""
        self.error = None
        self.log = []
        self.frames_done = 0
        self.frames_total = 0
        self.started = time.time()
        self.eta = None
        self._cancel = False
        self._thread = None
        self._proc = None
        # The card this job has rented, while it has one, and the money
        # this job may spend (set by the server when a session cap is on).
        self._rented = None
        self.budget_usd = None
        self.rental_cost = 0.0
        # Called with the cost of the card once it is given back, so the
        # session ledger is settled on what was spent, not on the estimate.
        self.on_rental_done = None
        self._lock = threading.Lock()

    # --- helpers -----------------------------------------------------------

    def say(self, msg):
        # A rental key must never reach the log, even by accident.
        msg = self._redact(msg)
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        with self._lock:
            self.log.append(line)
            if len(self.log) > 400:
                self.log = self.log[-400:]
        print(line, flush=True)

    def _redact(self, text):
        secret = self.opts.get("rent_key")
        if secret and len(secret) > 6:
            return str(text).replace(secret, "[redacted]")
        return text

    def _should_stop(self):
        """Why the rented work must stop now, or None to carry on.

        The cloud loops call this between pieces and between polls. Stop
        used to do nothing on a rented job: it set a flag nobody on the
        cloud path read, so the card kept working - and billing - until the
        whole job was done.
        """
        if self._cancel:
            return "cancelled"
        if self.budget_usd is not None and self._rented is not None:
            try:
                spent = self._rented.cost_so_far()
            except Exception:
                spent = 0.0
            if spent > self.budget_usd:
                return (f"stopped: this card has cost about ${spent:.2f}, past "
                        f"the ${self.budget_usd:.2f} the session cap left for "
                        f"this job. Ask the user before spending more.")
        return None

    def cancel(self):
        self._cancel = True
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass

    # --- the pipeline ------------------------------------------------------

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self.state = "running"
            self._do()
            if not self._cancel:
                self.state = "done"
                self.progress = 1.0
                self.stage = "done"
                self.say(f"done in {time.time() - self.started:.0f}s -> {self.out}")
        except Exception as e:
            self.error = str(e)
            self.state = "cancelled" if self._cancel else "error"
            self.say("ERROR: " + str(e))
        finally:
            self._cleanup()
            # A job that never rented a card (it failed or was refused
            # before the pod was created) still has its booking settled -
            # at nothing - so the ledger does not keep money it never spent.
            if self.on_rental_done and not getattr(self, "_settled", False):
                self._settled = True
                try:
                    self.on_rental_done(self.rental_cost)
                except Exception:
                    pass

    def _cleanup(self):
        for d in (os.path.join(self.work, "in"), os.path.join(self.work, "out")):
            shutil.rmtree(d, ignore_errors=True)

    def _run_cmd(self, cmd, **kw):
        if self._cancel:
            raise RuntimeError("cancelled")
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)

    def _do_star(self):
        """Run STAR, the diffusion video super-resolution model, in the cloud.

        STAR needs an NVIDIA GPU with 24 GB+ of VRAM. This Mac has neither
        CUDA nor that much memory, so the job goes to the official Hugging
        Face Space, which runs on a free ZeroGPU A10G.
        """
        import star

        opts = self.opts
        width = int(opts.get("star_width") or star.DEFAULT_WIDTH)
        fps = int(opts.get("star_fps") or star.DEFAULT_FPS)
        seconds = float(opts.get("star_seconds") or star.DEFAULT_SECONDS)
        start = float(opts.get("star_start") or 0)
        prompt = opts.get("star_prompt") or star.DEFAULT_PROMPT
        upscale = int(opts.get("star_upscale") or 4)
        chunk_len = int(opts.get("star_chunk_len") or 24)
        chunk_size = int(opts.get("star_chunk_size") or 3)
        token = opts.get("hf_token") or None

        clip = os.path.join(self.work, "star-input.mp4")
        self.stage = "preparing the clip"
        self.say(f"cutting {seconds:g}s from {start:g}s and scaling it to {width}px wide "
                 f"(STAR works on small clips)")
        star.prepare_clip(self.src, clip, start=start, seconds=seconds,
                          width=width, fps=fps)
        self.say(f"clip prepared: {os.path.getsize(clip)/1e6:.1f} MB")

        self.stage = "uploading to huggingface.co"
        self.progress = 0.1
        star.run(clip, self.out, prompt, upscale=upscale, chunk_len=chunk_len,
                 chunk_size=chunk_size, token=token, log=lambda m: self.say(m))

        self.progress = 1.0
        if os.path.exists(self.out):
            self.say(f"output: {os.path.getsize(self.out)/1e6:.1f} MB")

    def _do_cloud(self):
        """Split the job: the AI pass on a rented GPU, the rest on this Mac.

        The AI pass is the only part that wants a big GPU, and it is the
        only part worth paying for. The denoise, the blend, the softening,
        the grain, the grade and the encode stay here, because ffmpeg does
        them at many times real time and there is no sense in heating this
        Mac up or spending GPU seconds on them.

        If "rent it" is on, the card is rented for the length of this job
        and given back afterwards - even if the job fails.
        """
        import cloud
        import rental as rental_mod

        opts = self.opts
        engine = opts.get("cloud_engine") or "realesrgan"
        model = opts.get("model") or "xintao-realesr-animevideo-x2-v3"
        scale = int(opts.get("scale") or (4 if engine == "dloral" else 2))
        encoder = opts.get("encoder") or "h264"
        look_name = opts.get("look") or "clean"
        look = LOOKS.get(look_name) or LOOKS["clean"]
        denoise_engine = opts.get("denoise_engine") or "ffmpeg"

        # Before anything is spent: can this image even run this engine?
        #
        # The worker would say so - it answers /health with the truth - but
        # only after a card has been rented, a pod has booted and the
        # denoise has run. The diffusion engines are opt-in layers in the
        # image, so asking for one that is not there used to cost the whole
        # preparation and a pod before failing.
        image = opts.get("rent_image") or (
            rental_mod.DEFAULT_IMAGE_DLORAL if engine == "dloral"
            else rental_mod.DEFAULT_IMAGE_STAR if engine == "star"
            else rental_mod.DEFAULT_IMAGE_SEEDVR2
            if engine == "seedvr2"
            else rental_mod.DEFAULT_IMAGE)
        known = check_image_can_run(image, engine)
        self.say(f"the image {image} can run the {engine} engine"
                 + (f" - it has: {', '.join(known)}" if known else ""))

        info = video_info(self.src)
        self.say(f"source: {info['width']}x{info['height']} @ {info['fps']:.3f} fps, "
                 f"{info['duration']:.1f}s")

        # --- crop the letterbox -------------------------------------------
        crop = None
        if opts.get("crop") == "auto":
            crop = detect_crop(self.src)
            if crop:
                self.say(f"letterbox found: cropping to {crop[0]}x{crop[1]} "
                         f"at {crop[2]},{crop[3]}")
                info["width"], info["height"] = crop[0], crop[1]
            else:
                self.say("no letterbox to crop")

        # --- de-interlace -------------------------------------------------
        deint = ""
        mode = opts.get("deinterlace") or "auto"
        if mode != "off":
            # w3fdif is the best single-pass deinterlacer in ffmpeg; the
            # Topaz recipe uses QTGMC, which needs VapourSynth.
            interlaced = mode == "on" or info.get("field_order") in ("tt", "bb", "tb", "bt")
            if interlaced:
                deint = "w3fdif"
                self.say("de-interlacing with w3fdif (the frames were flagged interlaced)")
            elif mode == "auto":
                self.say("no de-interlace needed - the frames are already progressive")

        # --- how big should the result be? --------------------------------
        cap = opts.get("max_resolution") or ""
        target = target_size(info["width"], info["height"], cap) if cap else None
        if target:
            self.say(f"target size: {target[0]}x{target[1]} (inside {cap})")
        self.say(f"the AI pass goes to a {engine} worker; "
                 f"the finishing stays here (look {look_name})")

        # 1. the video that goes up: cropped, de-interlaced and denoised, so
        #    the AI never sees the black bars, the combing or the noise.
        upload = os.path.join(self.work, "upload.mp4")
        self.stage = "preparing the upload"
        src_info = video_info(self.src)
        extra = []
        if opts.get("crop") == "auto":
            crop = detect_crop(self.src)
            if crop:
                self.say(f"letterbox found: cropping to {crop[0]}x{crop[1]}")
                extra.append(f"crop={crop[0]}:{crop[1]}:{crop[2]}:{crop[3]}")
                src_info["width"], src_info["height"] = crop[0], crop[1]
        mode = opts.get("deinterlace") or "auto"
        if mode != "off":
            interlaced = mode == "on" or src_info.get("field_order") in ("tt", "bb", "tb", "bt")
            if interlaced:
                self.say("de-interlacing with w3fdif")
                extra.append("w3fdif")
        cap = opts.get("max_resolution") or ""
        target = target_size(src_info["width"], src_info["height"], cap) if cap else None
        if target:
            self.say(f"target size: {target[0]}x{target[1]} (inside {cap})")
        # Same rule as the local path: the model runs at its native scale and
        # the upload is pre-scaled so the result lands on the target.
        dtarget = None
        if engine == "dloral":
            # DLoRAL is a 4x model and it works from a canvas: the result
            # is four times the canvas. So the canvas is the target over
            # four, and the result lands ON the target - which is the size
            # of the source when there is no cap.
            #
            # Without this the whole source went up and the result came
            # back four times as big: a 1080p film would have asked for a
            # 4320p one, which no card can hold and nobody asked for.
            dtarget = target or (src_info["width"], src_info["height"])
            pre_w, pre_h = canvas_for(dtarget, src_info["width"],
                                      src_info["height"], scale)
            extra.append(f"scale={pre_w}:{pre_h}:flags=lanczos")
            self.say(f"the diffusion works from a {pre_w}x{pre_h} canvas, "
                     f"its {scale}x makes {pre_w*scale}x{pre_h*scale}, and "
                     f"the finishing scales that to {dtarget[0]}x{dtarget[1]}")
        elif engine == "seedvr2":
            # SeedVR2 restores to a resolution (--resolution, the short
            # side); it is not an N-times enlargement. So the guide must not
            # be cut down by a "native scale" - that used to run here with
            # the default model's 2x (or 4x), and a 1080p source capped at
            # 1080p went up as 960x540: three quarters of a real picture
            # thrown away for the model to invent back.
            #
            # The guide is the source as it is. It only comes down if it is
            # bigger than the target, and it is never blown up here: an
            # upscale adds no information, only upload bytes through a proxy
            # that gives a request about a hundred seconds.
            scale = 1
            pre_w, pre_h = guide_for_restore(src_info["width"], src_info["height"], target)
            if (pre_w, pre_h) != (even(src_info["width"]), even(src_info["height"])):
                extra.append(f"scale={pre_w}:{pre_h}:flags=lanczos")
            self.say(f"the guide is {pre_w}x{pre_h}: SeedVR2 restores to the "
                     f"target, so the guide keeps all the detail the source has")
        else:
            scale = native_scale_for(model)
            if target:
                pre = min(target[0] / (src_info["width"] * scale),
                          target[1] / (src_info["height"] * scale))
                pre_w = even(max(2, round(src_info["width"] * pre)))
                pre_h = even(max(2, round(src_info["height"] * pre)))
                extra.append(f"scale={pre_w}:{pre_h}:flags=lanczos")
                self.say(f"the AI runs at its native {scale}x on a "
                         f"{pre_w}x{pre_h} frame")
        motion = int(opts.get("motion") or 0)
        if motion > 1:
            import rife
            smoothed = os.path.join(self.work, "smoothed.mp4")
            _apply_motion(self.src, smoothed, motion, log=lambda m: self.say(m))
            extra = extra + []  # the smoothing already produced the frames
            upload_src = smoothed
        else:
            upload_src = self.src
        if denoise_engine == "bilateral" and look["denoise"]:
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
                log=lambda m: self.say(m))
        else:
            cloud.prepare_upload(upload_src, upload, denoise=look["denoise"] or None,
                                 extra_filters=extra)
        self.say(f"upload prepared: {os.path.getsize(upload)/1e6:.1f} MB")

        # STAR is the odd one out: it is not an enlargement of the film but
        # a regeneration of a short piece of it, and it works on a small
        # clip (its own example is 320px wide and two seconds). So the
        # clip is cut and shrunk here, on this Mac, and the card gets that.
        if engine == "star":
            import star as star_mod
            seconds = float(opts.get("star_seconds") or star_mod.DEFAULT_SECONDS)
            start = float(opts.get("star_start") or 0)
            width = int(opts.get("star_width") or star_mod.DEFAULT_WIDTH)
            star_mod.prepare_clip(self.src, upload, start=start, seconds=seconds,
                                  width=width, fps=star_mod.DEFAULT_FPS)
            self.say(f"STAR works on a small clip: {seconds:g}s from "
                     f"{start:g}s at {width}px wide "
                     f"({os.path.getsize(upload)/1e6:.1f} MB)")

        # 2. the AI pass, on the rented GPU.
        endpoint = opts.get("cloud_endpoint") or ""
        rented = None
        if opts.get("rent"):
            self.stage = "renting a GPU"
            self.progress = 0.05
            provider = opts.get("rent_provider") or "runpod"
            api_key = opts.get("rent_key") or ""
            if not api_key:
                # Fall back to the stored key: an environment variable, a 0600
                # file, or the macOS Keychain. The value is never logged.
                import keys
                api_key = keys.resolve(provider) or ""
            if not api_key:
                import keys
                where = "\n".join("  " + w for w in keys.where(provider))
                raise RuntimeError(
                    "no key for " + provider + ". Put it in one of these:\n" + where)
            # The card has to be able to hold the result. When the caller
            # did not name one, the engine gets the first card that can -
            # a DLoRAL job at 1080p needs 48 GB, and a 24 GB card would
            # die on the card, after the money is spent.
            rent_gpu = (opts.get("rent_gpu") or "").strip()
            if not rent_gpu:
                out_h = (target[1] if target else
                         (dtarget[1] if dtarget else info.get("height") or 1080))
                rent_gpu = rental_mod.default_gpu(engine, out_h)
            rented = rental_mod.make(
                provider=provider,
                engine=engine,
                api_key=api_key,
                gpu=rent_gpu or None,
                image=opts.get("rent_image") or None,
                region=opts.get("rent_region") or None,
                # Keep the card between jobs: the GPU is released and the
                # volume disk - with the weights on it - is kept, so the
                # next run of the same engine does not download them again.
                keep_warm=bool(opts.get("rent_keep_warm")),
                log=lambda m: self.say(m))
            # Nothing is rented without the explicit approval that came with
            # the estimate the user read.
            if not opts.get("rent_approved"):
                raise RuntimeError(
                    "this job would rent a GPU, and it has not been approved. "
                    "Ask for the estimate first.")
            estimate = opts.get("rent_estimate") or {}
            rented.approve(estimate)
            if estimate:
                self.say(f"approved: {estimate.get('gpu')} at "
                         f"${estimate.get('price_per_hour')}/h, "
                         f"about ${estimate.get('one_clip_cost')} for this clip"
                         + (f" ({_human_secs(estimate.get('ai_seconds'))} on the card)"
                            if estimate.get("ai_seconds") else "")
                         + ("" if estimate.get("measured", True)
                            else " - that number is a guess, the engine has "
                                 "never been measured on a card"))
            self._rented = rented
            rented.create()
            endpoint = rented.wait_ready()
            # The image has the last word on what it can do. The check above
            # is the project's own list; this is the image's.
            try:
                have = (cloud.health(endpoint) or {}).get("engines") or []
            except Exception:
                have = []
            if have and engine not in have:
                raise RuntimeError(
                    f"the worker on the card cannot run the {engine} engine - "
                    f"it has: {', '.join(have)}. That engine is an opt-in layer "
                    "in the image: build it with --build-arg BUILD_"
                    + engine.upper() + "=1 and point the GUI at that image, or "
                    "use the Polished preset, which the default image can run.")
            self.say(f"the worker can run: {', '.join(have) or 'unknown'}")

        try:
            self.stage = "the GPU is working"
            self.progress = 0.15
            ai = os.path.join(self.work, "ai.mp4")
            if engine == "dloral":
                # A diffusion pass over a film takes hours and the proxy
                # gives a request about a hundred seconds, so it cannot be
                # one request. The worker takes the video, answers with a
                # job id, and the pieces come back one at a time.
                cloud.dloral_run(endpoint, upload, ai, scale=scale,
                                 api_key=opts.get("cloud_key") or None,
                                 should_stop=self._should_stop,
                                 log=lambda m: self.say(m))
            elif engine == "star":
                # STAR: the steerable diffusion. Same shape - a job id and
                # the answer comes back when it is there - but the result is
                # one video, not a set of pieces.
                cloud.star_run(endpoint, upload, ai,
                               prompt=opts.get("prompt") or "",
                               upscale=int(opts.get("star_upscale") or scale or 4),
                               chunk_len=int(opts.get("star_chunk_len") or 24),
                               width=int(opts.get("star_width") or 320),
                               api_key=opts.get("cloud_key") or None,
                               should_stop=self._should_stop,
                               log=lambda m: self.say(m))
            else:
                resume_dir = resume_dir_for(
                    self.work, self.src, engine=engine, model=model, scale=scale,
                    resolution=(target[1] if target else SEEDVR2_DEFAULT_SHORT),
                    prompt=opts.get("prompt") or "",
                    # everything that shapes the upload
                    prep=extra, look_denoise=look.get("denoise") or "",
                    denoise_engine=denoise_engine, motion=motion,
                    bilateral=[opts.get("bilateral_radius"),
                               opts.get("bilateral_strength")],
                    chunk=cloud.CHUNK_FRAMES)
                cloud.upscale_chunked(endpoint, upload, ai, scale=scale, model=model,
                                      parts_dir=resume_dir,
                                      api_key=opts.get("cloud_key") or None,
                                      engine=engine,
                                      resolution=(target[1] if target
                                                   else SEEDVR2_DEFAULT_SHORT),
                                      prompt=opts.get("prompt") or None,
                                      should_stop=self._should_stop,
                                      log=lambda m: self.say(m))
        finally:
            # Give the card back whatever happened. An orphaned pod keeps
            # billing by the hour.
            if rented is not None:
                self.stage = "returning the GPU"
                try:
                    rented.destroy()
                finally:
                    try:
                        self.rental_cost = rented.cost_so_far()
                    except Exception:
                        self.rental_cost = 0.0
                    self.say(f"the card cost about ${self.rental_cost:.2f} "
                             f"(by this machine's clock)")
                    self._rented = None
                    if self.on_rental_done:
                        self._settled = True
                        try:
                            self.on_rental_done(self.rental_cost)
                        except Exception:
                            pass

        # The pieces have been joined into ai.mp4, so the resume folder has
        # done its job; a film's worth of pieces is gigabytes.
        if engine not in ("dloral", "star"):
            try:
                shutil.rmtree(resume_dir, ignore_errors=True)
            except Exception:
                pass

        # 3. the finishing, here.
        self.stage = "finishing on this Mac"
        self.progress = 0.85
        tmp = os.path.join(self.work, "finished.mp4")
        finish_look = dict(look)
        override = opts.get("blend_override")
        if override is not None and override != "":
            finish_look["blend"] = float(override)
        # The canvas has to be an even number of pixels - H.264 cannot
        # carry an odd one - so a 300 pixel tall target comes back as 296
        # and the result is four pixels short of where it was asked to be.
        # The finishing pass scales it to the target, so the file that
        # comes out is the size that was asked for.
        cloud.finish_locally(ai, upload, tmp, scale=scale, look=finish_look,
                             encoder=encoder, target=target or dtarget,
                             log=lambda m: self.say(m))
        shutil.move(tmp, self.out)
        self.say(f"output: {os.path.getsize(self.out)/1e6:.1f} MB")

    def _do(self):
        if self.opts.get("backend") == "star":
            return self._do_star()
        if self.opts.get("backend") == "cloud":
            return self._do_cloud()
        opts = self.opts
        model = opts.get("model") or "xintao-realesr-animevideo-x2-v3"
        scale = int(opts.get("scale") or 2)
        chunk_frames = int(opts.get("chunk_frames") or 30)
        encoder = opts.get("encoder") or "h264"
        look_name = opts.get("look") or "clean"
        look = LOOKS.get(look_name) or LOOKS["clean"]
        denoise = look["denoise"]
        denoise_engine = opts.get("denoise_engine") or "ffmpeg"
        bilateral_radius = int(opts.get("bilateral_radius") or 7)
        bilateral_strength = float(opts.get("bilateral_strength") or 0.85)

        info = video_info(self.src)
        self.say(f"source: {info['width']}x{info['height']} @ {info['fps']:.3f} fps, "
                 f"{info['vcodec']}, {info['duration']:.1f}s, {info['size']/1e6:.1f} MB")

        if not os.path.exists(BINARY):
            raise RuntimeError(f"the upscaler binary is missing: {BINARY}")

        # --- 1. crop, de-interlace, and normalise to CFR/CBR ----------------
        # All three happen in one pass, so the frames the AI sees are already
        # the frames we want.
        vf = []
        if opts.get("crop") == "auto":
            crop = detect_crop(self.src)
            if crop:
                self.say(f"letterbox found: cropping to {crop[0]}x{crop[1]} "
                         f"at {crop[2]},{crop[3]}")
                vf.append(f"crop={crop[0]}:{crop[1]}:{crop[2]}:{crop[3]}")
            else:
                self.say("no letterbox to crop")

        mode = opts.get("deinterlace") or "auto"
        if mode != "off":
            interlaced = mode == "on" or info.get("field_order") in ("tt", "bb", "tb", "bt")
            if interlaced:
                # w3fdif is the best single-pass deinterlacer in ffmpeg; the
                # Topaz recipe uses QTGMC, which needs VapourSynth.
                self.say("de-interlacing with w3fdif (the frames are flagged interlaced)")
                vf.append("w3fdif")
            elif mode == "auto":
                self.say("no de-interlace needed - the frames are already progressive")

        norm = os.path.join(self.work, "normalised.mp4")
        self.stage = "normalising"

        # Motion first, if asked for: the interpolated frames are what the
        # upscaler then enlarges.
        motion = int(opts.get("motion") or 0)
        source = self.src
        if motion > 1:
            smoothed = os.path.join(self.work, "smoothed.mp4")
            _apply_motion(self.src, smoothed, motion, log=lambda m: self.say(m))
            source = smoothed
            # The smoothed file runs at a different frame rate, so the
            # frame rate has to be read again - otherwise the normalisation
            # below would force it back to the original and throw away the
            # interpolated frames.
            info = video_info(source)
            self.say(f"the source is now {info['fps']:.3f} fps")

        self.say("normalising to constant frame rate / bit rate "
                 "(the upstream tool only handles CBR video)")
        cmd = [ffbin.ffmpeg(), "-y", "-v", "error", "-i", source]
        if vf:
            cmd += ["-vf", ",".join(vf)]
        # The bitrate follows the frame rate: interpolating doubles the frames
        # and needs twice the bits, or the normalisation becomes the thing
        # that ruins the picture.
        mbps = max(8, int(round(8 * info["fps"] / 25.0)))
        cmd += ["-fps_mode", "cfr", "-r", f"{info['fps']:.6f}",
                "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{mbps}M",
                "-maxrate", f"{mbps}M", "-bufsize", f"{mbps * 2}M",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", norm]
        res = self._run_cmd(cmd)
        if res.returncode != 0:
            # Not fatal: carry on with the original file.
            self.say("normalising failed, using the original file instead")
            norm = self.src
        else:
            self.say("normalised")

        info = video_info(norm)
        fps = info["fps"] if info["fps"] > 0 else 25.0

        # --- 1b. how big should the result be? ------------------------------
        cap = opts.get("max_resolution") or ""
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
        ai_h = even(pre_h * native)

        # --- 2. how many frames? ------------------------------------------
        self.stage = "counting"
        # Counting packets is much cheaper than decoding every frame.
        self.frames_total = _count_packets(norm)
        if not self.frames_total:
            self.frames_total = max(1, int(round(info["duration"] * fps)))
        self.say(f"{self.frames_total} frames at {fps:.3f} fps")

        # --- 3. one long-lived ffmpeg that reads the upscaled frames ------
        enc_args = ENCODERS.get(encoder, ENCODERS["h264"])[1]
        out_args = list(enc_args) + ["-r", f"{fps:.6f}"]
        ff = [ffbin.ffmpeg(), "-y", "-v", "error",
              "-f", "image2pipe", "-framerate", f"{fps:.6f}", "-vcodec", "png", "-i", "-",
              "-i", norm]

        # The treatment, applied to the upscaled frames.
        blend = float(look["blend"])
        override = opts.get("blend_override")
        if override is not None and override != "":
            # The "how soft do you want it" dial. QualityScaler and EnhancR
            # both have this as a slider - it is the standard way to take
            # the invented detail back out of an AI upscale.
            blend = float(override)
        soften = float(look["soften"])
        grain = int(look["grain"])
        contrast = float(look.get("contrast") or 0)
        saturation = float(look.get("saturation") or 0)
        cas = float(look.get("cas") or 0)
        halation = float(look.get("halation") or 0)
        # A small tscale is the slow one: the pattern then holds for a
        # second or two and drifts, the way emulsion does.
        perlin_grain = (look.get("grain_mode") == "perlin" and grain > 0
                        and ffbin.has_filter("perlin"))
        grain_s = grain * 0.05
        halation_sigma = max(4, min(24, (target[1] if target else ai_h) // 90))
        treat = bool(blend or soften or grain or contrast or saturation
                     or cas or halation
                     or look.get("chroma_shift") or look.get("chroma_soft")
                     or look.get("black_lift")
                     or float(look.get("gamma") or 1.0) != 1.0)

        if treat:
            # Two upscalers, blended: the AI result over a plain lanczos
            # enlargement of the source. The lanczos branch carries no
            # invented detail, so mixing it in is how the AI look gets taken
            # back out again.
            parts = [f"[0:v]setpts=PTS-STARTPTS,fps={fps:.6f}[ai]"]
            soft = "[1:v]"
            if denoise:
                soft += denoise + ","
            soft += f"scale={ai_w}:{ai_h}:flags=lanczos,setsar=1,fps={fps:.6f},setpts=PTS-STARTPTS[soft]"
            parts.append(soft)
            if blend > 0:
                parts.append(f"[ai][soft]blend=all_expr='A*(1-{blend})+B*{blend}'[mix]")
            else:
                parts.append("[ai]null[mix]")
            filters = []
            if target:
                # The AI gave us its own size; this is the one the user asked
                # for. A downscale here throws nothing away.
                filters.append(f"scale={target[0]}:{target[1]}:flags=lanczos")
            if soften:
                # unsharp takes: luma_msize_x:luma_msize_y:luma_amount:
                #             chroma_msize_x:chroma_msize_y:chroma_amount
                # A negative amount blurs instead of sharpening, which is
                # what "improve details and sharpening to -30" means.
                filters.append(f"unsharp=5:5:{soften}:5:5:{soften}")
            if cas:
                # cas is contrast adaptive sharpening: it brings an edge back
                # without the ringing that gives negative unsharp away.
                filters.append(f"cas=strength={cas}")
            if grain and not perlin_grain:
                # Static grain, the same pattern in every frame.
                #
                # Per-frame random noise ("temporal") is what Premiere's Add
                # Noise does, but on moving footage at 50 fps it crawls and
                # shimmers badly - the opposite of the calm film look. Static
                # grain gives the analogue texture without the flicker.
                filters.append(f"noise=alls={grain}:allf=u")
            filters.extend(grade_filters(look))
            filters.append("format=yuv420p")
            shift = int(look.get("chroma_shift") or 0)
            if shift:
                # The chroma axis is what makes a picture read as analogue:
                # the colour sits a sample behind the edges. The shift is in
                # chroma samples, so one is half a luma pixel in 4:2:0.
                filters.append(f"chromashift=cbh={shift}:crh={shift}:edge=smear")
            if look.get("chroma_soft"):
                filters.append("chromanr=thres=60:sizew=8:sizeh=8:stepw=2:steph=2")
            # A comma straight after a label is read as an empty filter, so
            # the filters are joined without one.
            parts.append("[mix]" + ",".join(filters) + "[v0]")
            # The grain and the halation are not here: they need a second
            # input, and a second input inside this piped encoder stalls the
            # framesync. They run as a post pass over the finished file.
            parts.append("[v0]null[v]")
            ff += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "1:a?"]
        else:
            ff += ["-map", "0:v", "-map", "1:a?"]
        # The perlin source is an endless lavfi stream, so without
        # -shortest the encoder waits for it forever and never
        # finalises the file.
        ff += out_args
        if look.get("grain_mode") == "perlin" or look.get("halation"):
            # A post pass follows, so this encode is only the intermediate.
            # Give it room; the post pass makes the final file.
            ff += ["-crf", "12"]
        ff += (["-shortest"] if perlin_grain else []) + [self.out]
        self.stage = "starting encoder"
        # stderr goes to a file, not a pipe: nobody reads a pipe while the
        # frames are being written, and a full pipe would deadlock ffmpeg.
        err_path = os.path.join(self.work, "encoder.log")
        err_file = open(err_path, "wb")
        self._proc = subprocess.Popen(ff, stdin=subprocess.PIPE,
                                      stdout=subprocess.DEVNULL, stderr=err_file)
        self._err_file = err_file

        # --- 4. chunks ----------------------------------------------------
        frame_in_dir = os.path.join(self.work, "in")
        frame_out_dir = os.path.join(self.work, "out")
        os.makedirs(frame_in_dir, exist_ok=True)
        os.makedirs(frame_out_dir, exist_ok=True)

        # How many frames per chunk. The 4K PNGs are ~10 MB each, so a big
        # chunk would need a lot of disk; 30 frames keeps the peak well
        # under a gigabyte while still amortising the model load.
        frames_per_chunk = max(1, int(opts.get("chunk_frames") or 30))
        chunk_index = 0
        first_frame = 0
        produced = 0
        t_start = time.time()

        while first_frame < self.frames_total:
            if self._cancel:
                raise RuntimeError("cancelled")
            count = min(frames_per_chunk, self.frames_total - first_frame)
            self.stage = f"upscaling chunk {chunk_index + 1}"
            self.progress = produced / self.frames_total

            # clean the chunk folders
            for d in (frame_in_dir, frame_out_dir):
                for f in os.listdir(d):
                    os.remove(os.path.join(d, f))

            start_time = first_frame / fps
            extract = [ffbin.ffmpeg(), "-y", "-v", "error", "-ss", f"{start_time:.4f}",
                       "-i", norm, "-frames:v", str(count)]
            vf = []
            if denoise and denoise_engine == "ffmpeg":
                # Denoise BEFORE the AI pass: an upscale amplifies mosquito
                # noise and MPEG blocking into "detail".
                vf.append(denoise)
            if (pre_w, pre_h) != (even(info["width"]), even(info["height"])):
                # Pre-scale, so the model's native factor lands on the target.
                vf.append(f"scale={pre_w}:{pre_h}:flags=lanczos")
            if vf:
                extract += ["-vf", ",".join(vf)]
            extract += ["-start_number", "1", "-f", "image2",
                        os.path.join(frame_in_dir, "%06d.png")]
            res = self._run_cmd(extract)

            if denoise and denoise_engine == "bilateral":
                # Lifecast's UpscaleVideo.ai de-noise: an OpenCV bilateral
                # filter mixed back with the original by a strength factor.
                import lifecast_denoise
                lifecast_denoise.process_dir(
                    frame_in_dir, radius=bilateral_radius,
                    strength=bilateral_strength, log=lambda m: self.say(m))
            got = len([f for f in os.listdir(frame_in_dir) if f.endswith(".png")])
            if got == 0:
                self.say(f"no frames extracted at {start_time:.2f}s, stopping")
                break

            # the AI pass - one process for the whole chunk.
            #
            # The binary prints a percentage, but it restarts that percentage
            # for every internal pass it makes, so it cannot be used as a
            # progress bar. Counting the files it writes out is unambiguous.
            up = subprocess.Popen(
                [BINARY, "-i", frame_in_dir, "-o", frame_out_dir,
                 "-s", str(scale), "-m", MODELS_DIR, "-n", model],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            tail = []

            def drain(stream, sink):
                for raw in stream:
                    sink.append(raw.decode("utf-8", "replace").rstrip())
                    if len(sink) > 40:
                        sink.pop(0)

            reader = threading.Thread(target=drain, args=(up.stdout, tail), daemon=True)
            reader.start()

            while up.poll() is None:
                if self._cancel:
                    up.terminate()
                    raise RuntimeError("cancelled")
                done_now = len([f for f in os.listdir(frame_out_dir) if f.endswith(".png")])
                self.progress = (produced + min(done_now, got)) / self.frames_total
                spent = time.time() - t_start
                if spent > 2 and self.progress > 0:
                    rate = (produced + min(done_now, got)) / spent
                    self.eta = (self.frames_total - produced - min(done_now, got)) / rate if rate else None
                time.sleep(0.35)
            up.wait()
            reader.join(timeout=2)
            if up.returncode != 0:
                raise RuntimeError("the upscaler failed:\n" + "\n".join(tail[-8:]))
            upscaled = sorted(f for f in os.listdir(frame_out_dir) if f.endswith(".png"))
            if not upscaled:
                raise RuntimeError("the upscaler produced no frames:\n" + "\n".join(tail[-8:]))

            # push them into the encoder
            for name in upscaled:
                with open(os.path.join(frame_out_dir, name), "rb") as fh:
                    self._proc.stdin.write(fh.read())
            self._proc.stdin.flush()

            produced += len(upscaled)
            first_frame += got
            chunk_index += 1

            elapsed = time.time() - t_start
            rate = produced / elapsed if elapsed else 0
            self.frames_done = produced
            self.eta = (self.frames_total - produced) / rate if rate else None
            self.progress = produced / self.frames_total
            RATES[model] = rate
            self.say(f"chunk {chunk_index}: {len(upscaled)} frames upscaled "
                     f"({produced}/{self.frames_total}, {rate:.2f} fps, "
                     f"eta {self.eta/60:.1f} min)")

        # --- 5. finish ----------------------------------------------------
        self.stage = "finishing"
        try:
            self._proc.stdin.close()
        except Exception:
            pass
        rc = self._proc.wait()
        try:
            self._err_file.close()
        except Exception:
            pass
        err = ""
        try:
            with open(err_path, "r", errors="replace") as fh:
                err = fh.read()[-800:]
        except Exception:
            pass
        if rc != 0 and not self._cancel:
            raise RuntimeError(f"ffmpeg failed ({rc}): {err}")

        if not os.path.exists(self.out):
            raise RuntimeError("ffmpeg produced no output file")

        # --- 6. grain and halation, as a post pass -------------------------
        look = LOOKS.get(self.opts.get("look") or "clean") or LOOKS["clean"]
        grain = int(look.get("grain") or 0)
        halation = float(look.get("halation") or 0)
        perlin = look.get("grain_mode") == "perlin" and grain > 0
        if perlin and not ffbin.has_filter("perlin"):
            self.say("this ffmpeg has no perlin source (it needs 7.1 or later) - "
                     "the grain falls back to a static pattern")
            perlin = False
        if grain or halation:
            try:
                _post_pass(self, look, grain, halation, perlin)
            except Exception as e:
                # A look that fails to finish should not throw away the
                # upscale that already worked.
                self.say(f"the post pass did not run: {e}")

        if os.path.exists(self.out):
            self.say(f"output: {os.path.getsize(self.out)/1e6:.1f} MB")


def _post_pass(self, look, grain, halation, perlin):
    """Grain and halation, over the finished file.

    The picture is already encoded at this point, so both inputs are files
    and the filtergraph is quick. The grain is perlin when the look asks for
    it - a noise field with continuity in space and time, so it drifts the
    way emulsion does instead of sitting still like dirt on a lens.
    """
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
         "-show_format", self.out], stdout=subprocess.PIPE).stdout.decode())
    try:
        dur = float(info.get("format", {}).get("duration") or 0) or 12.0
    except Exception:
        dur = 12.0
    v = next(s for s in info["streams"] if s.get("codec_type") == "video")
    w, h = int(v["width"]), int(v["height"])
    try:
        num, den = (v.get("avg_frame_rate") or "25/1").split("/")
        fps = float(num) / float(den) if float(den) else 25.0
    except Exception:
        fps = 25.0

    inputs = []
    chain = []
    node = "[0:v]"
    if halation:
        # Halation is light scattering inside the film: a soft glow around
        # the highlights. It must only ever ADD light. The earlier version
        # added a blurred copy arithmetically, which SUBTRACTS below
        # mid-grey - it crushed the shadows (the 25th percentile fell from
        # 11 to 5) and made the picture look heavy and strange.
        #
        # So: threshold the highlights, blur them, dim them, and screen
        # them back. Screen can only add, so the shadows are untouched, and
        # the glow carries the colour of the highlight it came from.
        sigma = max(4, min(24, h // 90))
        thresh = 210
        gain = halation * 2.0
        chain.append("[0:v]split=2[base][hlsrc]")
        # The threshold applies to EVERY plane, so the chroma of the halo
        # becomes zero and contributes nothing when it is screened back.
        # The halo is the blurred HIGHLIGHTS only: threshold first, then
        # blur, so the glow cannot spread into the shadows.
        #
        # The add is written so that it can only ever ADD: max(0, ...) floors
        # it at zero. The three attempts before this each failed in a
        # measurable way - an arithmetic blend subtracted below mid-grey
        # (1st percentile 0 -> 5), screening a blurred copy lifted the
        # whole picture (0 -> 45), and maskedmerge replaced the highlights
        # and dimmed them (99th percentile 161 -> 145).
        chain.append(f"[hlsrc]format=gray,gblur=sigma={sigma}[hlg]")
        chain.append(f"[hlg]lut='if(gt(val,{thresh}),val,0)'[hlm]")
        # The glow is added, but it may not push the picture past white: a
        # studio light that was already bright would go flat and lose its
        # texture, which is worse than no glow at all.
        chain.append(f"[base][hlm]blend=c0_expr='min(235,A+max(0,B-128)*{halation})':"
                     f"c1_expr='A':c2_expr='A'[v1]")
        node = "[v1]"
    if grain:
        if perlin:
            # The pattern is rendered once into a small file and looped:
            # three seconds of texture that drifts and then repeats.
            gw, gh = max(2, w // 4), max(2, h // 4)
            grain_file = os.path.join(self.work, "grain.gray")
            if not os.path.exists(grain_file):
                # As long as the picture, and no longer: a looping input
                # gives the framesync an endless supply to chew on, and it
                # spins at full tilt instead of finishing.
                subprocess.run(
                    [ffbin.ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i",
                     f"perlin=size={gw}x{gh}:rate={fps:.6f}:"
                     f"tscale={look.get('grain_tscale') or 0.08}:octaves=2:"
                     f"persistence=0.5:random_mode=seed:random_seed=1",
                     "-t", f"{dur:.3f}", "-pix_fmt", "gray", "-f", "rawvideo",
                     grain_file], check=True, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE)
            # The pattern has a mean of its own - about 149 at these
            # settings, not 128 - so it has to be measured and subtracted.
            # Without this the "grain" is a uniform lift of the picture.
            try:
                # The WHOLE file, not the first megabyte: the perlin field
                # starts bright and settles, so a short sample reads about
                # 20 levels high and the correction over-subtracts (the
                # picture came out five levels dark).
                total = 0
                count = 0
                with open(grain_file, "rb") as fh:
                    while True:
                        block = fh.read(4 * 1024 * 1024)
                        if not block:
                            break
                        total += sum(block)
                        count += len(block)
                bias = (total / max(1, count)) - 128.0
            except Exception:
                bias = 0.0
            self.say(f"the grain pattern is biased by {bias:+.1f} levels - "
                     f"that is subtracted")
            inputs = ["-f", "rawvideo", "-pixel_format", "gray",
                      "-video_size", f"{gw}x{gh}", "-framerate", f"{fps:.6f}",
                      "-i", grain_file]
            chain.append("[1:v]scale=%d:%d:flags=bilinear,format=yuv420p[bn]" % (w, h))
            strength = grain * 0.05
        else:
            # The frozen pattern that ffmpeg's own noise filter leaves
            # behind. Kept as the default because it is instant.
            chain.append(f"{node}noise=alls={grain}:allf=u[v1]")
            strength = 0.0
        if perlin:
            # The result is kept inside the legal range. The grain is a plus
            # or a minus, and a plus on a picture that is already near white
            # would clip it to a flat, dead white.
            chain.append(f"{node}[bn]blend=c0_expr='min(235,max(16,"
                         f"A+(B-128-({bias:.3f}))*{strength}))':"
                         f"c1_expr='A':c2_expr='A'[v2]")
            node = "[v2]"

    tmp = os.path.join(self.work, "post.mp4")
    enc = ENCODERS.get(self.opts.get("encoder") or "h264", ENCODERS["h264"])[1]
    cmd = [ffbin.ffmpeg(), "-y", "-v", "error", "-i", self.out] + inputs + \
          ["-filter_complex", ";".join(chain), "-map", node, "-map", "0:a?",
           "-c:a", "copy"] + list(enc) + [tmp]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0 or not os.path.exists(tmp):
        raise RuntimeError(res.stderr.decode("utf-8", "replace")[-400:])
    shutil.move(tmp, self.out)
    self.say("grain and halation applied")


def preset_availability(preset, image=None):
    """Can this preset run with the image we have?

    The diffusion engines are opt-in layers in the image, so a preset that
    needs one is a dead end until that image is built. It is better to say
    so on the button - with the reason - than to find out after the denoise
    and after a pod has been rented.

    The job checks this again for itself, against the image that is
    actually going to be used; this one is for the button.
    """
    import rental as rental_mod

    backend = preset.get("backend")
    name = preset.get("cloud_engine")
    if backend == "star":
        name = "star"
    if backend not in ("cloud", "star"):
        return {"available": True}
    img = image or (rental_mod.DEFAULT_IMAGE_DLORAL if name == "dloral"
                    else rental_mod.DEFAULT_IMAGE_STAR if name == "star"
                    else rental_mod.DEFAULT_IMAGE_SEEDVR2
                    if name == "seedvr2"
                    else rental_mod.DEFAULT_IMAGE)
    known = rental_mod.engines_for(img)
    if known and name not in known:
        return {"available": False,
                "reason": f"the image {img} has no {name} engine - it is an "
                          f"opt-in layer in the image (BUILD_{name.upper()}=1). "
                          f"Point the image field at one that has it: "
                          f"bzz1/upscaler:both"}
    return {"available": True}


# The engines that are diffusion rather than enlargement. They have a
# scale of their own and do not use a Real-ESRGAN model at all, so the
# scale check against that model does not apply to them.
DIFFUSION_ENGINES = ("seedvr2", "dloral", "star")


def resume_dir_for(job_work, source, **settings):
    """Where a cloud job keeps its finished pieces, so a retry can use them.

    A retry from the GUI is a NEW job with a new folder, so pieces kept in
    the job's own folder were never found again - which is why a job that
    died redid every piece. The folder is named after the source file (its
    path, size and modification time) and every setting that shapes the
    upload and the answer, so the next job finds it only when it is asking
    for the same thing of the same file.

    Not after the upload's bytes: the upload is re-encoded each time and
    x264's rate control with threads does not give the same bytes twice.
    The frames are the same, and a seam between an old piece and a new one
    was measured invisible (84 old + 22 new pieces: a frame difference of
    10.9 at the join, against a median of 17.1 across the film).
    """
    import hashlib
    st = os.stat(source)
    ident = {"source": os.path.abspath(source), "size": st.st_size,
             "mtime": int(st.st_mtime), **settings}
    key = hashlib.sha256(json.dumps(ident, sort_keys=True, default=str).encode())
    root = os.path.join(os.path.dirname(os.path.abspath(job_work)), "_resume")
    return os.path.join(root, key.hexdigest()[:20])


def guide_for_restore(src_w, src_h, target=None):
    """The guide a restoring engine (SeedVR2) is given.

    The source as it is, brought down only when it is bigger than the
    target - never down to target/N, and never blown up.
    """
    if not target:
        return (even(src_w), even(src_h))
    k = min(1.0, target[0] / src_w, target[1] / src_h)
    return (even(max(2, round(src_w * k))), even(max(2, round(src_h * k))))


def canvas_for(target, src_w, src_h, scale):
    """The canvas a model has to be given so its own scale lands on the target.

    A model that multiplies by `scale` has to be given a picture that is
    `scale` times smaller than the result, or the result comes back
    `scale` times too big. The size is rounded to even numbers, because
    H.264 cannot carry an odd one.
    """
    pre = min(target[0] / (src_w * scale), target[1] / (src_h * scale))
    return (even(max(2, round(src_w * pre))), even(max(2, round(src_h * pre))))


# The short side the SeedVR2 diffusion is asked for when there is no cap.
# The job, the estimate and the GUI all have to use the same number, so it
# lives here and they read it from here.
SEEDVR2_DEFAULT_SHORT = 1080


def output_size_for(engine, src_w, src_h, cap=None):
    """The size the result will be, for the engine that is going to make it.

    The engines do not agree on this, which is why the GUI cannot just
    multiply the source by the scale:

    * the Real-ESRGAN models enlarge the picture, so the result is the
      source times the model's own factor;
    * SeedVR2 is asked for the target's short side, or 1080 when there is
      no cap - it restores rather than enlarges;
    * DLoRAL works from a canvas a quarter of the result, so the result is
      the target: the cap, or the size of the source when there is no cap.
    """
    if cap:
        cw, ch = (int(v) for v in str(cap).lower().split("x"))
        k = min(cw / src_w, ch / src_h)
        return (even(max(2, round(src_w * k))), even(max(2, round(src_h * k))))
    if engine == "dloral":
        return (even(src_w), even(src_h))
    if engine == "seedvr2":
        short = SEEDVR2_DEFAULT_SHORT
        k = min((src_w * short / src_h) / src_w, short / src_h)
        return (even(max(2, round(src_w * k))), even(max(2, round(src_h * k))))
    return (even(src_w), even(src_h))


def check_image_can_run(image, engine):
    """Raise unless this image has the engine the job needs.

    An unknown image gets the benefit of the doubt: the worker knows
    best, and it can be asked once the pod is up.
    """
    import rental as rental_mod
    known = rental_mod.engines_for(image)
    if known and engine not in known:
        raise RuntimeError(
            f"the image {image} cannot run the {engine} engine - it has: "
            + ", ".join(known) + ". That engine is an opt-in layer in the "
            "image: build it with --build-arg BUILD_"
            + engine.upper() + "=1 and point the GUI at that image, or "
            "use the Polished preset, which the default image can run.")
    return known


def _human_secs(s):
    try:
        s = float(s or 0)
    except Exception:
        return "?"
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    if h >= 48:
        return f"{h // 24} days {h % 24} h"
    if h:
        return f"{h} h {m:02d} m"
    if m:
        return f"{m} m"
    return f"{int(s)} s"


def estimate_rental(opts, src):
    """What a job would cost if it rented a GPU. Nothing is spent.

    The numbers come from the card's live price and the measured speed of
    the model, so the figure in front of the user is the same one the job
    will actually run against.
    """
    import rental as rental_mod
    provider = opts.get("rent_provider") or "runpod"
    engine = opts.get("cloud_engine") or "realesrgan"
    gpu = opts.get("rent_gpu") or None
    image = opts.get("rent_image") or None
    region = opts.get("rent_region") or None
    try:
        api_key = opts.get("rent_key") or ""
        if not api_key:
            import keys
            api_key = keys.resolve(provider) or ""
        if not api_key:
            return {"error": "no key for " + provider, "where": keys.where(provider)}
        r = rental_mod.make(provider=provider, engine=engine, api_key=api_key,
                            gpu=gpu, image=image, region=region,
                            log=lambda m: None)
    except Exception as e:
        return {"error": str(e)}
    try:
        info = video_info(src)
        frames = int(round((info.get("duration") or 0) * (info.get("fps") or 25)))
    except Exception:
        frames = 0
    # The diffusion engines work at the resolution they are producing, so
    # the estimate has to be priced by that, not by the size of the source.
    # The output is the target when there is one, and otherwise the size of
    # the source - the same numbers the job uses, so the quote and the bill
    # agree.
    out_w = out_h = None
    if engine == "star":
        # STAR is not an enlargement of the film: it regenerates a short
        # piece of it, from a small clip, and it makes a picture that is
        # the clip times the factor. So the estimate is about the clip -
        # its size and its length - and not about the film.
        import star as star_mod
        seconds = float(opts.get("star_seconds") or star_mod.DEFAULT_SECONDS)
        width = int(opts.get("star_width") or star_mod.DEFAULT_WIDTH)
        up = int(opts.get("star_upscale") or opts.get("scale") or 4)
        clip_w = width
        clip_h = int(round(clip_w * (info.get("height") or 9)
                           / max(1, info.get("width") or 16)))
        out_w = clip_w * up
        out_h = clip_h * up
        frames = min(frames, int(round(seconds * star_mod.DEFAULT_FPS)))
    elif engine != "realesrgan":
        cap = opts.get("max_resolution") or ""
        target = target_size(info.get("width") or 640,
                             info.get("height") or 480, cap) if cap else None
        if target:
            out_h = target[1]
        elif engine == "dloral":
            # DLoRAL's canvas is the target over four, and with no cap the
            # target is the source - so the result is the size of the
            # source, not four times it.
            out_h = info.get("height") or 1080
        else:
            out_h = SEEDVR2_DEFAULT_SHORT
        out_w = int(round(out_h * (info.get("width") or 16)
                          / max(1, info.get("height") or 9)))
    # When nobody names a card, the one the engine gets has to be able to
    # HOLD the result: a DLoRAL job that ends at 1080p does not fit on a
    # 24 GB card, and the estimate must not quote a card that cannot do
    # the work.
    if not (opts.get("rent_gpu") or "").strip():
        try:
            import engine as engine_mod
            if engine in engine_mod.DIFFUSION_ENGINES:
                r.gpu = rental_mod.default_gpu(engine, out_h)
        except Exception:
            pass
    try:
        plan = r.plan(frames=frames, fps=info.get("fps") or 25.0, engine=engine,
                      width=info.get("width") or 640, height=info.get("height") or 480,
                      model=opts.get("model") or None,
                      out_width=out_w, out_height=out_h)
    except Exception as e:
        return {"error": str(e)}
    return plan


def default_model_for(width, height):
    """A sane default for TV footage: the video-tuned model at 2x."""
    return "xintao-realesr-animevideo-x2-v3"


# Measured throughput, frames per second, per model. Filled in by the jobs
# themselves, so the UI can quote a real number instead of a guess.
RATES = {}


def measured_rate(model):
    return RATES.get(model)


def available_models():
    out = []
    for mid, (label, max_scale, note, native) in MODELS.items():
        if not os.path.exists(os.path.join(MODELS_DIR, mid + ".bin")):
            continue
        # The size of the .param file tracks the depth of the network, and
        # the depth is what the run time is made of.
        param = os.path.join(MODELS_DIR, mid + ".param")
        depth = 0
        if os.path.exists(param):
            with open(param, "rb") as fh:
                depth = fh.read().count(b"\n")
        out.append({"id": mid, "label": label, "max_scale": max_scale,
                    "note": note, "native_scale": native, "depth": depth,
                    "rate": RATES.get(mid)})
    return out
