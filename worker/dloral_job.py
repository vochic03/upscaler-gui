#!/usr/bin/env python3
"""DLoRAL as a background job.

The pod's proxy gives a request about a hundred seconds, and a diffusion
pass over a whole film takes hours - so the job cannot live inside the
request. The worker takes the video, answers straight away with a job id,
and does the work in the background. The Mac polls for it and downloads the
pieces one at a time, because the answer for a whole film at four times
the size would be far too big for one response.

DLoRAL itself loads its model once for a whole directory of videos, so the
video is cut into pieces and handed over in batches: one model load for a
batch of pieces, not one per piece.
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
# proxy, so it has to be small enough to fetch and big enough that the
# per-piece overhead does not dominate.
PIECE_FRAMES = int(os.environ.get("DLORAL_PIECE_FRAMES", "20"))
# How many pieces one DLoRAL invocation takes. The frames of a whole batch
# are held in memory while the model runs, so this is a memory limit, and
# each batch costs one model load.
BATCH_PIECES = int(os.environ.get("DLORAL_BATCH_PIECES", "6"))
# The scale of the diffusion.
SCALE = int(os.environ.get("DLORAL_SCALE", "4"))


def _run(cmd, cwd=None, timeout=None):
    return subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=timeout)


def made_frames(out_root, stem):
    """The frames the DLoRAL script MADE, for one piece.

    The script writes two sets of frames: the ones it read, next to the
    video it read them from, and the ones it made, in the output
    directory under the video's name. Picking up the first set gives back
    the untouched canvas and throws the diffusion away - the job looks
    like it worked, and the result is the input with a new bitrate.

    The frames it made are numbered from one, so there is no frame_0000.
    """
    return sorted(glob.glob(os.path.join(out_root, stem, "frame_*.png")))


class Job:
    def __init__(self, video_path, scale=SCALE, log=print):
        self._print = log
        self.id = uuid.uuid4().hex[:12]
        self.state = "queued"
        self.error = None
        self.scale = scale
        self.video = video_path
        self.work = tempfile.mkdtemp(prefix=f"dloral-{self.id}-")
        self.in_dir = os.path.join(self.work, "in")
        self.out_dir = os.path.join(self.work, "pieces")
        # Where the DLoRAL script puts the frames it MADE. It is not the
        # directory the video went into: the script writes the frames it
        # READ next to that video, and the diffusion result under the
        # video's name in here. Picking up the wrong set gives back the
        # untouched canvas and throws the diffusion away.
        self.script_out = os.path.join(self.work, "dloral-out")
        # The lines the job writes are kept as well as printed: a card can
        # be busy for half an hour - fetching eleven gigabytes of weights -
        # and the Mac asks the worker what it is doing. Without this the
        # only thing the user sees is "the worker is working on it".
        self.lines = []
        self.log = self._record
        self.pieces = []      # [{"name", "state": pending|done|failed, "bytes"}]
        self.fps = 25.0       # the frame rate of the video that came in
        self.started = time.time()
        self.finished = None
        self.thread = None

    # --- the status the Mac polls ---------------------------------------
    def _record(self, msg):
        """Print a line, and keep it for whoever asks how it is going."""
        self.lines.append(str(msg))
        if len(self.lines) > 200:
            del self.lines[:-200]
        self._print(msg)

    def status(self):
        done = sum(1 for p in self.pieces if p["state"] == "done")
        return {"job": self.id, "state": self.state,
                "done": done, "total": len(self.pieces),
                "error": self.error,
                "log": self.lines[-80:],
                "seconds": round((self.finished or time.time()) - self.started),
                "pieces": [{"name": p["name"], "state": p["state"],
                            "bytes": p.get("bytes")} for p in self.pieces]}

    def piece_path(self, name):
        if name not in {p["name"] for p in self.pieces}:
            return None
        path = os.path.join(self.out_dir, name)
        return path if os.path.exists(path) else None

    # --- the work --------------------------------------------------------
    def start(self):
        # The caller may delete the file it handed over as soon as it has
        # the job id, so take a copy first.
        self.local_video = os.path.join(self.work, "source.mp4")
        shutil.copy(self.video, self.local_video)
        self.video = self.local_video
        self.thread = threading.Thread(target=self._run_job, daemon=True)
        self.thread.start()
        return self

    def _split(self):
        """Cut the video into pieces, on frame boundaries."""
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
        # The result has to keep the frame rate it came in at. A 50 fps
        # source written out at 25 fps is a film at half speed.
        self.fps = fps
        for i, start in enumerate(range(0, frames, PIECE_FRAMES)):
            end = min(start + PIECE_FRAMES, frames)
            name = f"piece-{i:05d}.mp4"
            # The DLoRAL script cannot make the FIRST frame of the video it
            # is given: it works on a two-frame window and makes the second
            # one, using the first for temporal context. So the first frame
            # of every piece comes back missing - and a film cut into
            # thirty-frame pieces loses thirty frames to that, one at every
            # seam, and drifts away from its own audio.
            #
            # The fix is one frame of overlap: every piece is cut from one
            # frame earlier, so the frame that goes missing is the overlap
            # and not a real frame of the film. The first piece has nothing
            # before it, so it gets a copy of its own first frame instead -
            # the same predecessor, made of the same picture.
            if start > 0:
                cut_start = start - 1
                vf = f"trim=start_frame={cut_start}:end_frame={end},setpts=PTS-STARTPTS"
            else:
                cut_start = 0
                vf = (f"trim=start_frame=0:end_frame={end},setpts=PTS-STARTPTS,"
                      f"tpad=start_mode=clone:start_duration={1.0/fps:.6f}")
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", self.video,
                   "-vf", vf,
                   "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "14",
                   "-pix_fmt", "yuv420p", "-r", f"{fps:.6f}",
                   os.path.join(self.in_dir, name)]
            r = _run(cmd, timeout=600)
            if r.returncode != 0:
                raise RuntimeError("could not cut the video:\n"
                                   + r.stdout.decode("utf-8", "replace")[-400:])
            self.pieces.append({"name": name, "state": "pending", "bytes": 0,
                                # The piece has to come back with this many
                                # frames, and the count is checked when it is
                                # encoded: a film that is a frame short at
                                # every seam drifts away from its audio.
                                "frames": end - start})
        self.log(f"cut into {len(self.pieces)} pieces of {PIECE_FRAMES} frames")

    def _weights_ready(self, models):
        """Fetch the weights if they are not in the image already.

        They are eleven gigabytes, so the image does not carry them by
        default: the card fetches them the first time it needs them.
        """
        # Where the weights actually live, once every link is resolved. This
        # is the difference between "fetched once onto the volume" and
        # "fetched again on every run", and it is worth saying out loud.
        self.log("the DLoRAL weights live at " + os.path.realpath(models))
        if os.path.exists(os.path.join(models, "checkpoints", "model.pkl")):
            self.log("the weights are already there - nothing to fetch")
            return
        script = os.environ.get("DLORAL_FETCH", "/app/fetch_dloral_weights.sh")
        if not os.path.exists(script):
            raise RuntimeError(
                "the DLoRAL weights are missing and there is nothing to "
                "fetch them with: " + models)
        self.log("fetching the DLoRAL weights (this happens once)")
        r = _run(["/bin/sh", script], timeout=3600)
        if r.returncode != 0:
            raise RuntimeError("the weights could not be fetched:\n"
                               + r.stdout.decode("utf-8", "replace")[-500:])
        self.log("the weights are in")

    def _made_frames(self, stem):
        """The frames the DLoRAL script made for this piece."""
        return made_frames(self.script_out, stem)

    def _dloral_batch(self, batch):
        """One DLoRAL invocation over a batch of pieces."""
        repo = os.environ.get("DLORAL_REPO", "/app/DLoRAL")
        models = os.environ.get("DLORAL_MODELS", "/app/DLoRAL/preset/models")
        python = os.environ.get("DLORAL_PYTHON", "/app/venvs/dloral/bin/python")
        sd = os.environ.get("DLORAL_SD",
                             "/app/DLoRAL/preset_models/stable-diffusion-2-1-base")
        self._weights_ready(models)
        staging = os.path.join(self.work, "batch")
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging, exist_ok=True)
        for p in batch:
            shutil.copy(os.path.join(self.in_dir, p["name"]),
                        os.path.join(staging, p["name"]))
        out = self.script_out
        cmd = [python, "src/test_DLoRAL.py",
               "--pretrained_model_path", sd,
               "--pretrained_model_name_or_path", sd,
               "--ram_path", os.path.join(models, "ram_swin_large_14m.pth"),
               "--ram_ft_path", os.path.join(models, "DAPE.pth"),
               "--pretrained_path", os.path.join(models, "checkpoints", "model.pkl"),
               "--process_size", "512",
               "--upscale", str(self.scale),
               "--load_cfr",
               "--stages", "1",
               "--merge_and_unload_lora", "False",
               "-i", staging,
               "-o", out]
        r = _run(cmd, cwd=repo)
        if r.returncode != 0:
            # The script writes its frames next to the video it read, with
            # four digits, and the output directory is not where they go.
            # If it says nothing, the answer "no frames" on its own says
            # nothing either - so the log of the run is kept. The exception
            # is at the HEAD of it and the frames are at the tail, so both
            # ends are kept: the tail alone is a wall of stack frames with
            # the reason somewhere above the cut.
            out = r.stdout.decode("utf-8", "replace")
            raise RuntimeError("DLoRAL failed:\n" + out[:400] +
                               ("\n...\n" if len(out) > 1200 else "") +
                               out[-800:])
        for p in batch:
            stem = os.path.splitext(p["name"])[0]
            # The frames the script MADE - not the ones it read.
            frames = self._made_frames(stem)
            if not frames:
                p["state"] = "failed"
                self.error = (self.error or "") + f" {stem}: no frames"
                continue
            self._encode(frames, p, os.path.join(out, stem))

    def _encode(self, frames, piece, frame_dir):
        """The PNGs go back into a video."""
        os.makedirs(self.out_dir, exist_ok=True)
        path = os.path.join(self.out_dir, piece["name"])
        # 4320p is past what H.264 officially covers, so the big ones go
        # out as HEVC.
        w = h = 0
        try:
            info = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=width,height", "-of", "csv=p=0", frames[0]],
                stdout=subprocess.PIPE).stdout.decode().strip()
            w, h = [int(x) for x in info.split(",")[:2]]
        except Exception:
            pass
        big = w * h > 4096 * 2304
        # The frames the script made are numbered from ONE, not from zero:
        # it numbers them by the index of the frame they were made from, and
        # the first one is the second frame of the piece. Asking ffmpeg for
        # frame_0000.png finds nothing and the encode fails.
        first = 1
        try:
            first = int(os.path.basename(frames[0]).split("_")[1].split(".")[0])
        except Exception:
            pass
        fps = self.fps or 25.0
        cmd = ["ffmpeg", "-y", "-v", "error", "-framerate", f"{fps:.6f}",
               "-start_number", str(first),
               "-i", os.path.join(frame_dir, "frame_%04d.png")]
        if big:
            cmd += ["-c:v", "libx265", "-crf", "18", "-preset", "medium",
                    "-pix_fmt", "yuv420p", "-tag:v", "hvc1"]
        else:
            cmd += ["-c:v", "libx264", "-crf", "17", "-preset", "medium",
                    "-pix_fmt", "yuv420p"]
        cmd += ["-r", f"{fps:.6f}", "-movflags", "+faststart", path]
        r = _run(cmd, timeout=1800)
        if r.returncode != 0 or not os.path.exists(path):
            piece["state"] = "failed"
            self.error = (self.error or "") + " " + (r.stdout.decode("utf-8", "replace")[-300:])
            return
        # Did it come back with the frames it should have? The DLoRAL
        # script makes one frame fewer than it was given - it cannot make
        # the first frame of a window - and the overlap in the cut is what
        # puts that frame on the overlap instead of on the film. If the
        # count is still short, the seam would eat a real frame.
        want = piece.get("frames")
        got = 0
        try:
            info = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                 "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1",
                 path], stdout=subprocess.PIPE).stdout.decode()
            for line in info.splitlines():
                if line.startswith("nb_read_frames="):
                    got = int(line.split("=")[1])
        except Exception:
            got = 0
        if want and got and got != want:
            self.log(f"{piece['name']}: {got} frames, expected {want} "
                     f"- the piece is {want - got} short")
        piece["state"] = "done"
        piece["bytes"] = os.path.getsize(path)

    def _run_job(self):
        try:
            self.state = "running"
            self._split()
            for i in range(0, len(self.pieces), BATCH_PIECES):
                batch = self.pieces[i:i + BATCH_PIECES]
                self._dloral_batch(batch)
                done = sum(1 for p in self.pieces if p["state"] == "done")
                self.log(f"  {done}/{len(self.pieces)} pieces done "
                         f"({time.time() - self.started:.0f}s)")
            self.state = "failed" if self.error else "done"
        except Exception as e:
            self.error = str(e)
            self.state = "failed"
            self.log("the job failed: " + str(e))
        finally:
            self.finished = time.time()


def start(video_path, scale=SCALE, log=print):
    job = Job(video_path, scale=scale, log=log).start()
    with LOCK:
        JOBS[job.id] = job
    return job


def get(job_id):
    with LOCK:
        return JOBS.get(job_id)
