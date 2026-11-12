#!/usr/bin/env python3
"""STAR as a background job.

STAR is the steerable diffusion: it takes a short clip and a sentence
describing what is in it, and regenerates the clip at a higher
resolution. The model works in chunks and wants a lot of VRAM - about
39 GB for its own small example - so the card has to be a big one.

The pod's proxy gives a request about a hundred seconds and the
inference takes longer than that, so the job cannot live inside the
request. The worker takes the clip, answers straight away with a job id,
and does the work in the background. The Mac polls for it and downloads
the result when it is there.

The weights (7.6 GB) are fetched on the card the first time they are
needed - see fetch_star_weights.sh.
"""
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

JOBS = {}
LOCK = threading.Lock()

# How long the inference may take before it is given up on. The card is
# slow and the model is big; two hours is generous.
TIMEOUT = int(os.environ.get("STAR_TIMEOUT", "7200"))
# The name the result is written under.
OUT_NAME = "clip-upscaled.mp4"


def _run(cmd, cwd=None, timeout=None):
    return subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=timeout)


class Job:
    def __init__(self, video_path, prompt="", upscale=4, chunk_len=24,
                 width=320, log=print):
        self.id = uuid.uuid4().hex[:12]
        self.state = "queued"
        self.error = None
        self.prompt = prompt or ""
        self.upscale = int(upscale or 4)
        self.chunk_len = int(chunk_len or 24)
        self.width = int(width or 320)
        self.video = video_path
        self.work = tempfile.mkdtemp(prefix=f"star-{self.id}-")
        self.out_dir = os.path.join(self.work, "out")
        # The lines the job writes are kept as well as printed: STAR fetches
        # seven and a half gigabytes of weights on a cold card, and the Mac
        # asks the worker what it is doing.
        self._print = log
        self.lines = []
        self.log = self._record
        self.thread = None
        self.started = time.time()
        self.finished = None

    # --- the status the Mac polls ---------------------------------------
    def _record(self, msg):
        """Print a line, and keep it for whoever asks how it is going."""
        self.lines.append(str(msg))
        if len(self.lines) > 200:
            del self.lines[:-200]
        self._print(msg)

    def status(self):
        done = 1 if self.state == "done" else 0
        return {"job": self.id, "state": self.state, "done": done,
                "total": 1, "error": self.error,
                "log": self.lines[-80:],
                "seconds": round((self.finished or time.time()) - self.started),
                "pieces": [{"name": OUT_NAME, "state": self.state,
                            "bytes": self._size()}]}

    def _size(self):
        p = os.path.join(self.out_dir, OUT_NAME)
        try:
            return os.path.getsize(p)
        except OSError:
            return 0

    def result_path(self):
        return os.path.join(self.out_dir, OUT_NAME)

    def piece_path(self, name):
        """The finished video, for the polling route."""
        if name != OUT_NAME:
            return None
        path = self.result_path()
        return path if os.path.exists(path) else None

    # --- the work ---------------------------------------------------------
    def _weights_ready(self):
        """Fetch the weights if they are not on the card already."""
        models = os.environ.get("STAR_WEIGHTS", "/app/STAR/pretrained_weight")
        target = os.path.join(models, "model.pt")
        # Where the weights really are, once the links are resolved: on the
        # volume disk they survive the pod, on the container disk they do
        # not. Say which one this is.
        self.log("the STAR weights live at " + os.path.realpath(models))
        if os.path.exists(target) and os.path.getsize(target) > 0:
            self.log("the weights are already there - nothing to fetch")
            return target
        script = os.environ.get("STAR_FETCH", "/app/fetch_star_weights.sh")
        if not os.path.exists(script):
            raise RuntimeError(
                "the STAR weights are missing and there is nothing to fetch "
                "them with: " + target)
        self.log("fetching the STAR weights (this happens once)")
        r = _run(["sh", script], timeout=TIMEOUT)
        if r.returncode != 0 or not os.path.exists(target):
            raise RuntimeError("the STAR weights could not be fetched:\n"
                               + r.stdout.decode("utf-8", "replace")[-500:])
        self.log("the weights are in")
        return target

    def _run_inference(self, model_path):
        repo = os.environ.get("STAR_REPO", "/app/STAR")
        python = os.environ.get("STAR_PYTHON", "/app/venvs/star/bin/python")
        os.makedirs(self.out_dir, exist_ok=True)
        cmd = [python, "video_super_resolution/scripts/inference_sr.py",
               "--input_path", self.video,
               "--save_dir", self.out_dir,
               "--file_name", OUT_NAME,
               "--model_path", model_path,
               "--prompt", self.prompt or "a good video",
               "--upscale", str(self.upscale),
               "--max_chunk_len", str(self.chunk_len),
               "--solver_mode", "fast",
               "--steps", "15"]
        self.log("running the inference: " + " ".join(cmd[1:]))
        r = _run(cmd, cwd=repo, timeout=TIMEOUT)
        if r.returncode != 0:
            out = r.stdout.decode("utf-8", "replace")
            # The exception is at the HEAD of the log and the progress is
            # at the tail, so both ends are kept.
            raise RuntimeError("STAR failed:\n" + out[:400] +
                               ("\n...\n" if len(out) > 1200 else "") +
                               out[-800:])
        made = self.result_path()
        if not os.path.exists(made):
            raise RuntimeError("STAR said nothing went wrong, but there is no "
                               "result in " + self.out_dir)
        return made

    def _run_job(self):
        try:
            self.state = "running"
            model_path = self._weights_ready()
            self._run_inference(model_path)
            self.state = "done"
        except Exception as e:
            self.state = "error"
            self.error = str(e)[:1500]
        finally:
            self.finished = time.time()

    def start(self):
        # The caller may delete the file it handed over as soon as it gets
        # the job id - and the worker does exactly that, because the upload
        # lives in a temporary directory that is cleaned up straight away.
        # So take a copy first, and read that. Without it the inference
        # finds an empty file and stops with "stack expects a non-empty
        # TensorList".
        self.local_video = os.path.join(self.work, "source.mp4")
        shutil.copy(self.video, self.local_video)
        self.video = self.local_video
        self.thread = threading.Thread(target=self._run_job, daemon=True)
        self.thread.start()
        return self


def start(video_path, prompt="", upscale=4, chunk_len=24, width=320, log=print):
    job = Job(video_path, prompt=prompt, upscale=upscale, chunk_len=chunk_len,
              width=width, log=log).start()
    with LOCK:
        JOBS[job.id] = job
    return job


def get(job_id):
    with LOCK:
        return JOBS.get(job_id)
