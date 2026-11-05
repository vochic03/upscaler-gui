#!/usr/bin/env python3
"""A diffusion pass as a background job.

Two engines, one shape:

  * **SeedVR2** (ByteDance, the official ComfyUI release) - a 3B
    transformer, one step, temporally consistent. It takes a directory of
    videos and writes the enhanced ones next to them. Its cost follows the
    resolution of the *output*: 480p is about a fifth of 1080p.
  * **DLoRAL** - the same idea with the Stable Diffusion 2.1 UNet. Its
    frames come out as PNGs and have to be encoded back into a video.

Neither fits in a request: the pod's proxy gives one about a hundred
seconds and a diffusion pass over a film takes hours. So the worker takes
the video, answers with a job id, and does the work in the background. The
Mac polls for the pieces and downloads them one at a time, because the
answer for a whole film would be far too big for one response.
"""
import glob
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

JOBS = {}
LOCK = threading.Lock()

# How many frames go into one piece. The piece is what comes back over the
# proxy, so it has to be small enough to fetch.
PIECE_FRAMES = int(os.environ.get("DIFF_PIECE_FRAMES", "50"))
# The batch size has to follow the 4n+1 rule, and it wants to match the
# shot length for the temporal consistency to work.
BATCH_SIZE = int(os.environ.get("DIFF_BATCH_SIZE", "5"))


def _run(cmd, cwd=None, timeout=None):
    return subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=timeout)


def _read_exact(stream, n):
    out = b""
    while len(out) < n:
        chunk = stream.read(n - len(out))
        if not chunk:
            break
        out += chunk
    return out


class DiffusionJob:
    def __init__(self, video_path, engine="seedvr2", resolution=None,
                 batch_size=None, scale=4, log=print):
        self.id = uuid.uuid4().hex[:12]
        self.state = "queued"
        self.error = None
        self.engine = engine
        self.video = video_path
        self.resolution = resolution
        self.batch_size = batch_size or BATCH_SIZE
        self.scale = scale
        self.work = tempfile.mkdtemp(prefix=f"diff-{self.id}-")
        self.in_dir = os.path.join(self.work, "in")
        self.out_dir = os.path.join(self.work, "out")
        # The lines the job writes are kept as well as printed, so the Mac
        # can show what the card is doing while it works.
        self._print = log
        self.lines = []
        self.log = self._record
        self.pieces = []
        self.started = time.time()
        self.finished = None
        self.thread = None

    # --- what the Mac polls ---------------------------------------------
    def _record(self, msg):
        """Print a line, and keep it for whoever asks how it is going."""
        self.lines.append(str(msg))
        if len(self.lines) > 200:
            del self.lines[:-200]
        self._print(msg)

    def status(self):
        done = sum(1 for p in self.pieces if p["state"] == "done")
        return {"job": self.id, "state": self.state, "engine": self.engine,
                "done": done, "total": len(self.pieces), "error": self.error,
                "log": self.lines[-80:],
                "seconds": round((self.finished or time.time()) - self.started),
                "pieces": [{"name": p["name"], "state": p["state"],
                            "bytes": p.get("bytes")} for p in self.pieces]}

    def piece_path(self, name):
        if name not in {p["name"] for p in self.pieces}:
            return None
        path = os.path.join(self.out_dir, name)
        return path if os.path.exists(path) else None

    def start(self):
        self.local_video = os.path.join(self.work, "source.mp4")
        shutil.copy(self.video, self.local_video)
        self.video = self.local_video
        self.thread = threading.Thread(target=self._run_job, daemon=True)
        self.thread.start()
        return self

    # --- the work --------------------------------------------------------
    def _split(self):
        os.makedirs(self.in_dir, exist_ok=True)
        # The frame count and the frame rate, read as key=value pairs: the
        # order ffprobe prints them in is the stream's own, not the order
        # they are written in.
        info = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
             "-show_entries", "stream=nb_read_frames,avg_frame_rate", "-of",
             "default=nw=1", self.video], stdout=subprocess.PIPE).stdout.decode()
        frames = 0
        fps = 25.0
        for line in info.splitlines():
            line = line.strip()
            if line.startswith("nb_read_frames="):
                try:
                    frames = int(line.split("=")[1])
                except ValueError:
                    frames = 0
            elif line.startswith("avg_frame_rate="):
                try:
                    num, den = line.split("=")[1].split("/")
                    fps = float(num) / float(den) if float(den) else 25.0
                except Exception:
                    fps = 25.0
        for i, start in enumerate(range(0, frames, PIECE_FRAMES)):
            end = min(start + PIECE_FRAMES, frames)
            name = f"piece-{i:05d}.mp4"
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", self.video,
                   "-vf", f"trim=start_frame={start}:end_frame={end},"
                          "setpts=PTS-STARTPTS",
                   "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "14",
                   "-pix_fmt", "yuv420p", "-r", f"{fps:.6f}",
                   os.path.join(self.in_dir, name)]
            r = _run(cmd, timeout=600)
            if r.returncode != 0:
                raise RuntimeError("could not cut the video:\n"
                                   + r.stdout.decode("utf-8", "replace")[-400:])
            self.pieces.append({"name": name, "state": "pending", "bytes": 0})
        self.log(f"cut into {len(self.pieces)} pieces of {PIECE_FRAMES} frames")

    def _seedvr2(self):
        repo = os.environ.get("SEEDVR2_REPO", "/app/seedvr2")
        models = os.environ.get("SEEDVR2_MODEL_DIR", "/app/seedvr2_models/SEEDVR2")
        script = os.environ.get("SEEDVR2_FETCH", "/app/fetch_seedvr2_weights.sh")
        if not os.path.exists(os.path.join(models,
                                           "seedvr2_ema_3b_fp8_e4m3fn.safetensors")):
            if os.path.exists(script):
                self.log("fetching the SeedVR2 weights (this happens once)")
                r = _run(["/bin/sh", script], timeout=3600)
                if r.returncode != 0:
                    raise RuntimeError("the weights could not be fetched:\n"
                                       + r.stdout.decode("utf-8", "replace")[-500:])
            else:
                raise RuntimeError("the SeedVR2 weights are missing: " + models)
        os.makedirs(self.out_dir, exist_ok=True)
        cmd = ["python3", "inference_cli.py", self.in_dir,
               "--output", self.out_dir,
               "--output_format", "mp4",
               # The ffmpeg writer, not OpenCV's: it writes a proper H.264
               # file, and it is the one the worker's own SeedVR2 route
               # uses. OpenCV's mp4v writer needs codec support that a
               # headless build does not always have, and the failure only
               # shows up after the model has already run.
               "--video_backend", "ffmpeg",
               "--model_dir", models,
               "--dit_model", "seedvr2_ema_3b_fp8_e4m3fn.safetensors",
               "--batch_size", str(self.batch_size),
               "--color_correction", "lab"]
        if self.resolution:
            cmd += ["--resolution", str(self.resolution)]
        self.log("running SeedVR2: " + " ".join(cmd))
        r = _run(cmd, cwd=repo, timeout=36000)
        if r.returncode != 0:
            raise RuntimeError("SeedVR2 failed:\n"
                               + r.stdout.decode("utf-8", "replace")[-800:])
        for p in self.pieces:
            stem = os.path.splitext(p["name"])[0]
            # Where the CLI puts the result depends on how it was told to
            # write. Given an --output DIRECTORY it writes the file under
            # its own name ("piece-00000.mp4"); without one it writes it
            # next to the input with "_upscaled" in the name. Both are
            # possible, so both are looked for.
            made = None
            for candidate in (f"{stem}_upscaled.mp4", f"{stem}.mp4"):
                path = os.path.join(self.out_dir, candidate)
                if os.path.exists(path):
                    made = path
                    break
            if not made:
                p["state"] = "failed"
                self.error = (self.error or "") + f" {stem}: no output"
                continue
            shutil.move(made, os.path.join(self.out_dir, p["name"]))
            p["state"] = "done"
            p["bytes"] = os.path.getsize(os.path.join(self.out_dir, p["name"]))

    def _dloral(self):
        from dloral_job import Job as DloralJob
        # DLoRAL has its own job class; run it in place.
        inner = DloralJob(self.video, scale=self.scale, log=self.log)
        inner.start()
        inner.thread.join()
        self.error = inner.error
        os.makedirs(self.out_dir, exist_ok=True)
        for p in inner.pieces:
            name = p["name"]
            src = os.path.join(inner.out_dir, name)
            if os.path.exists(src):
                shutil.move(src, os.path.join(self.out_dir, name))
                p["state"] = "done"
                p["bytes"] = os.path.getsize(os.path.join(self.out_dir, name))
        self.pieces = inner.pieces

    def _run_job(self):
        try:
            self.state = "running"
            self._split()
            if self.engine == "dloral":
                self._dloral()
            else:
                self._seedvr2()
            self.state = "failed" if self.error else "done"
        except Exception as e:
            self.error = str(e)
            self.state = "failed"
            self.log("the job failed: " + str(e))
        finally:
            self.finished = time.time()


def start(video_path, engine="seedvr2", resolution=None, batch_size=None,
          scale=4, log=print):
    job = DiffusionJob(video_path, engine=engine, resolution=resolution,
                       batch_size=batch_size, scale=scale, log=log).start()
    with LOCK:
        JOBS[job.id] = job
    return job


def get(job_id):
    with LOCK:
        return JOBS.get(job_id)
