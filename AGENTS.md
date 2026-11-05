# AGENTS.md - how to work in this project

Everything here was paid for with time or money on 2026-11-02. Read it
before a heavy command, not after.

## The rules that are not negotiable

1. **Never rent a GPU without an estimate in advance and the user's
   approval.** State the price per hour, the spin-up, and the projected
   total before the pod is created. An estimate that has not been checked
   against a bill is a guess.
2. **The cloud routes need an approval id.** The server issues one with
   the estimate; the user approves it (the GUI, or `POST /api/approve`).
   An agent may ask for a price; it may not approve its own spending.
3. **Destroy every pod.** `rental.destroy()` in a `finally`, and the
   pod ids are written to `~/.upscaler-gui-pods.json` so a crash cannot
   orphan one - the server sweeps that file when it starts.
4. **Do not restart the GUI while a job runs.** The job's thread dies
   with the process, the `finally` never runs, and the pod keeps billing.
   A restart mid-job cost $0.38 once.
5. **Restart the GUI after editing `server.py`, `engine.py` or
   `rental.py`** - the running server holds the old code in memory. This
   cost a verification run and a 400 ("network volume not found").

## What costs what

| thing | cost | note |
|---|---|---|
| the Docker patch build | 45 s, ~50 MB | `docker build -f Dockerfile.patch -t bzz1/upscaler:both .` |
| the Docker full build | ~90 min under QEMU, ~50 GB of VM disk | only for a structural change |
| DLoRAL at 1080p | 10.45 s a frame, 48 GB VRAM | the canvas is the target over four, so the detail is invented at 480x270 |
| DLoRAL at 720p | 24 GB VRAM | ~$3.15 for a 72-second film |
| SeedVR2 at 1080p | 1.68 s a frame, **48 GB VRAM** | it OOMs on a 24 GB card |
| SeedVR2 at 720p | 24 GB VRAM | |
| the weights fetch | 5-72 MB/s from Hugging Face | measured twice, a 14x spread |
| the card pool | minutes | the 48 GB cards were empty for 20 minutes at a stretch |

## The limits that bite

* **The proxy gives a request about 100 seconds** and uploads run at
  roughly 90 KB/s. A 12 MB piece died with a 524. The cloud route
  therefore sends **50-frame pieces** (`CHUNK_FRAMES` in `cloud.py`).
* **The Colima data disk is 100 GB** and holds the Docker store. A full
  build needs ~50 GB of it. When the disk filled, the containerd store
  was corrupted and every image looked broken.
* **The base image needs its manifest pulled by digest**, and the
  **amd64** one:
  `docker pull --platform linux/amd64 nvidia/cuda@sha256:2541299cf78eee8ee2b415782ad6f083bc4d351c4e40f104c776159bd483650e`
  A tag-only pull leaves the builder hunting for a digest it never
  stored, and the arm64 manifest has no torch 2.5.1.
* **Runpod's card names are not what you would guess**: `NVIDIA RTX
  A5000` (not "NVIDIA GeForce RTX A5000") and `NVIDIA A100 80GB PCIe`
  (not "NVIDIA A100 PCIe"). A wrong name fails at the moment of
  renting. `rental.catalogue_ids()` lists the real ones.
* **ffmpeg can break mid-job.** Homebrew replaces it and its libraries
  one at a time; in between, the binary cannot load its own libvpx and
  every call dies with a loader error. `ffbin.ffmpeg()` picks one that
  actually runs.
* **The server only reads from its library roots** (`~/Movies`,
  `~/Videos`, `~/Downloads`, `~/Desktop`, `~/drm-capture`, `work/`). A
  file elsewhere has to be copied in.

## The services

* the GUI: `python3 server.py --port 8790` (127.0.0.1 only)
* the local worker: `cd worker && UPSCALER_BIN=../vendor/upscaler/bin/mac-amd64 UPSCALER_MODELS=../vendor/upscaler/models PORT=8791 python3 server.py`

## Before you touch anything

```bash
python3 tools/test_cloud_guards.py    # 39 checks, all of them real
python3 tools/volume.py list          # the network volume (off by default)
python3 tools/insights.py             # the OpenRouter spend ledger
```

## What is deliberately off

* **The network volume** is off by default: $2.80 a month against about
  $0.05 a run. Turn it on with `tools/volume.py create` only if the
  same engine runs thirty times a month.
* **Sharing with the public catalogue** is off: the operations point at
  127.0.0.1, so there is nothing to share.
