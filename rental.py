#!/usr/bin/env python3
"""
Rent a GPU for the length of one job, then give it back.

The point: the AI pass needs a card that this Mac does not have. Rather
than keeping one running (and paying for it while it sits idle), the
rental exists only while the job does.

    create  ->  wait until the worker answers  ->  run the job  ->  destroy

The destroy is in a `finally`, so the card is given back even if the job
fails or you press Stop. That is the whole reason this is a module and not
three lines in the job: an orphaned pod keeps billing you by the hour.

Providers:

  runpod  - the real thing. A pod from our container image, reached over
            its published port, terminated when the job is done.
  local   - starts the worker as a process on this machine. Not a rental,
            but it exercises the exact same lifecycle, so the flow can be
            tested without an account.
"""

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, "worker", "server.py")

RUNPOD_API = "https://api.runpod.io/v2"
# Cloudflare looks at the fingerprint of the client, and Python's default
# one does not pass. The browser header is what gets through.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

def gpu_choices(engine="realesrgan", api_key=None, out_height=None):
    """The cards to offer, with the price they are at right now.

    The price in the label used to be typed in by hand, and it went stale:
    the dropdown said $0.44/h for a card the catalogue was asking $0.89/h
    for, and the estimate below it said the other number. The catalogue
    knows the price, so it is asked, and the number in the label is the
    number the estimate will use.

    The cards are also filtered by the memory the engine needs. A card
    that cannot hold the picture is not a choice: the job dies on the
    card, after the money is spent.
    """
    import re
    choices = [dict(c) for c in
               (GPU_CHOICES.get(engine) or GPU_CHOICES["realesrgan"])]
    need = vram_for(engine, out_height)
    if need:
        choices = [c for c in choices if (c.get("vram") or 0) >= need]
    prices = catalogue_prices(api_key)
    for c in choices:
        p = prices.get(c["id"])
        if p:
            c["label"] = re.sub(r"\$\d+(\.\d+)?/h", "$%.2f/h" % p, c["label"])
            c["price_per_hour"] = p
    return choices


def default_gpu(engine="realesrgan", out_height=None):
    """The card this engine gets when nobody names one.

    It has to be a card that can HOLD the picture: a DLoRAL job that ends
    at 1080p does not fit on a 24 GB card, and a default that cannot do
    the job is a job that dies on the card, after the money is spent.
    """
    for c in GPU_CHOICES.get(engine) or GPU_CHOICES["realesrgan"]:
        need = vram_for(engine, out_height)
        if not need or (c.get("vram") or 0) >= need:
            return c["id"]
    return (GPU_CHOICES.get(engine) or GPU_CHOICES["realesrgan"])[0]["id"]


def vram_for(engine, out_height=None):
    """How much VRAM this engine needs to make a picture this tall.

    DLoRAL's VAE decoder runs its attention at the SIZE OF THE RESULT,
    so the memory it wants follows the picture and not the source: at
    1080p the attention alone is 16 GB, and the pass does not fit on a
    24 GB card at all. A 720p result is a quarter of the pixels, which
    puts it inside a 24 GB card - that is worked out from the size of
    the attention rather than measured, and it is why a capped result
    can be made on a cheap card.

    STAR is a 1.7B transformer and wanted about 39 GB for its own small
    example, so it needs the big cards whatever the size.

    The SeedVR2 numbers are the ones it was measured on: a 3B model in
    FP8 on a 24 GB card at 1080p.
    """
    h = int(out_height or 1080)
    if engine == "dloral":
        return 24 if h <= 720 else 48
    if engine == "star":
        return 48
    if engine == "seedvr2":
        # At 1080p its video VAE runs out of memory on a 24 GB card
        # ("Allocation on device" -> torch.OutOfMemoryError), even though
        # the same size fitted when it was measured. Give it the big
        # cards above 720p and let the small ones do the small work.
        return 24 if h <= 720 else 48
    return 0


def catalogue_ids(api_key=None):
    """The card names Runpod actually has.

    They are not what you would guess: the A5000 is "NVIDIA RTX A5000" and
    the 80 gigabyte A100 is "NVIDIA A100 80GB PCIe". A name that does not
    exist fails at the moment the card is rented, with a message about
    global networking that says nothing about the real problem.
    """
    if not api_key:
        try:
            import keys
            api_key = keys.resolve("runpod")
        except Exception:
            return set()
    if not api_key:
        return set()
    try:
        req = urllib.request.Request(
            RUNPOD_API + "/catalog/gpus",
            headers={"Authorization": "Bearer " + api_key,
                     "Content-Type": "application/json",
                     "User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode() or "{}")
        return {g.get("id") for g in data.get("gpus", []) if g.get("id")}
    except Exception:
        return set()


def catalogue_prices(api_key=None):
    """The live price per hour of every card in the catalogue.

    An empty dict when the catalogue cannot be reached - the caller then
    keeps whatever it had.
    """
    if api_key is None:
        try:
            import keys
            api_key = keys.resolve("runpod")
        except Exception:
            api_key = None
    if not api_key:
        return {}
    try:
        req = urllib.request.Request(
            RUNPOD_API + "/catalog/gpus",
            headers={"Authorization": "Bearer " + api_key,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode() or "{}")
    except Exception:
        return {}
    out = {}
    for g in data.get("gpus", []):
        p = g.get("price") or {}
        # The secure price is what a pod normally gets; the community price
        # is the spot one. Take the dearer.
        prices = [float(p[k]) for k in ("secure", "community") if p.get(k)]
        if prices:
            out[g.get("id")] = max(prices)
    return out


# What to ask for, per engine, and how much memory the card has.
# The list is filtered by what the engine needs for the size of the
# result, so the 24 GB cards turn up for a DLoRAL job that is not going
# past 720p - and stay away from one that is.
GPU_CHOICES = {
    "realesrgan": [
        {"id": "NVIDIA GeForce RTX 4090", "label": "RTX 4090 24 GB — $0.44/h", "vram": 24},
        {"id": "NVIDIA RTX A5000", "label": "RTX A5000 24 GB — $0.44/h", "vram": 24},
        {"id": "NVIDIA L40S", "label": "L40S 48 GB — $0.99/h", "vram": 48},
        {"id": "NVIDIA A100 80GB PCIe", "label": "A100 80 GB — $1.19/h", "vram": 80},
    ],
    "dloral": [
        # The 24 GB cards are here for the small results: a DLoRAL job
        # that ends at 720p or below fits on one of them.
        {"id": "NVIDIA GeForce RTX 4090", "label": "RTX 4090 24 GB — $0.44/h", "vram": 24},
        {"id": "NVIDIA RTX A5000", "label": "RTX A5000 24 GB — $0.44/h", "vram": 24},
        {"id": "NVIDIA L40S", "label": "L40S 48 GB — $0.99/h", "vram": 48},
        {"id": "NVIDIA A100 80GB PCIe", "label": "A100 80 GB — $1.19/h", "vram": 80},
        {"id": "NVIDIA H100 PCIe", "label": "H100 80 GB — $2.90/h", "vram": 80},
    ],
    # STAR wants about 39 GB of VRAM for its own small example, so the
    # 24 GB cards are not on the list.
    "star": [
        {"id": "NVIDIA L40S", "label": "L40S 48 GB — $0.99/h", "vram": 48},
        {"id": "NVIDIA A100 80GB PCIe", "label": "A100 80 GB — $1.19/h", "vram": 80},
        {"id": "NVIDIA H100 PCIe", "label": "H100 80 GB — $2.90/h", "vram": 80},
    ],
}

# The container that has the worker in it. It has to be pushed to a
# registry the rental service can pull from.
DEFAULT_IMAGE = os.environ.get("UPSCALER_IMAGE", "docker.io/bzz1/upscaler:latest")
# The diffusion engines are opt-in layers, so each one has an image of its
# own. This is the image DLoRAL runs in - the one with the diffusion layer
# in it. It used to point at the plain image, which has no DLoRAL, and that
# is what made the Regenerate preset a dead end: the preset said the image
# could not run the engine, and the job said the same thing and refused
# before it began.
DEFAULT_IMAGE_DLORAL = os.environ.get("UPSCALER_IMAGE_DLORAL",
                                      "docker.io/bzz1/upscaler:both")
# STAR lives in the same image as the other diffusion engines, but the
# plain image has no STAR in it, so it gets its own default.
DEFAULT_IMAGE_STAR = os.environ.get("UPSCALER_IMAGE_STAR",
                                    "docker.io/bzz1/upscaler:both")
# SeedVR2 has an image of its own too.
DEFAULT_IMAGE_SEEDVR2 = os.environ.get("UPSCALER_IMAGE_SEEDVR2",
                                       "docker.io/bzz1/upscaler:seedvr2")

# The network volume the weights live on.
#
# It is the answer to "every run downloads eleven gigabytes again": the
# weights are fetched onto the volume once, and every pod that mounts it
# finds them already there. It costs $0.07 per GB per month - $2.80 for
# the forty gigabytes the three engines need - and it saves $0.38 a run.
#
# A pod can only mount a volume from its own data centre, so the card is
# rented where the volume is - which narrows the choice of card to that
# one centre. If there is no card of the kind you want there, the job
# falls back to a card without the volume and the weights are fetched
# onto the card's own disk instead (slower, but it works).
#
# The data centre matters more than it looks. Of the twenty-four centres
# that take a network volume, US-TX-3 is the one that had an L40S in it
# when this was set up; US-NC-1, where the volume was first created, had
# no GPU of any kind and could never have used it.
#
# IT IS OFF BY DEFAULT, and that is a money decision:
#
#   the volume            $2.80 a month, every month, used or not
#   what it saves         one weight fetch per run
#   the fetch             11.8 GB at 72 MB/s = 2 min 43 s = $0.05
#                         (the same fetch measured 5-9 MB/s on another
#                          run, which would make it $0.79)
#   the break-even        about 30 runs a month
#
# A card kept warm between jobs keeps its weights on its own disk, which
# costs nothing and covers the same ground for jobs that come one after
# another. So the volume is a thing you turn on when your own usage
# justifies it, and the tool that turns it on is tools/volume.py:
#
#   python3 tools/volume.py create --dc US-TX-3 --size 40
#   # and put the id it prints into RUNPOD_VOLUME_ID
DEFAULT_VOLUME_ID = os.environ.get("RUNPOD_VOLUME_ID", "")

# What each image can actually do.
#
# The worker knows this - it writes the list into the image at build time and
# answers it on /health - but asking the worker means a card has been rented
# and a pod has booted, which is exactly the money we are trying not to
# spend on a job that cannot run. So the project keeps the same list for the
# images it pushes, and the job checks it before anything is spent.
#
# The diffusion engines are opt-in layers in the Dockerfile (BUILD_DLORAL,
# BUILD_SEEDVR2, BUILD_STAR), so the image that is pushed by default has
# none of them.
IMAGE_ENGINES = {
    "docker.io/bzz1/upscaler:latest": ("realesrgan", "face"),
    "docker.io/bzz1/upscaler:v2": ("realesrgan",),
    "docker.io/bzz1/upscaler:v3": ("realesrgan", "face"),
    # Built with --build-arg BUILD_SEEDVR2=1 and pushed on 2026-10-31.
    # The list is what the worker on the card answered on /health.
    "docker.io/bzz1/upscaler:seedvr2": ("realesrgan", "face", "seedvr2"),
    # Both diffusion engines and STAR. Pushed on 2026-10-31; the list is
    # what the worker on the card answered on /health.
    "docker.io/bzz1/upscaler:both": ("realesrgan", "face", "dloral", "seedvr2",
                                     "star"),
}

# The weights each diffusion engine needs, in bytes, and the images that
# carry them inside themselves.
#
# The seedvr2 image was built with BUILD_SEEDVR2_WEIGHTS=1, so its three
# gigabytes come down with the image - at the fifty megabytes a second the
# image CDN serves them, not the five to nine that Hugging Face serves
# them at. The other two are fetched on the card, which is twenty minutes
# of a card's time on every cold run.
WEIGHTS_BYTES = {
    "seedvr2": 3_400_000_000,
    "dloral": 11_800_000_000,
    "star": 7_600_000_000,
}

IMAGE_BAKED_WEIGHTS = {
    "docker.io/bzz1/upscaler:seedvr2": ("seedvr2",),
    "docker.io/bzz1/upscaler:both": (),
}

# How fast the weights come down from Hugging Face onto a card.
#
# Measured twice, and the two measurements are a long way apart:
#   2026-11-01, a pod in EU-RO-1: 11.8 GB in 163 s = 72 MB/s
#   an earlier run from the same session: 5-9 MB/s
# The difference is probably the CDN: a file that is already cached near
# the card comes down at the speed of the cache, and one that is not comes
# down at the speed of the origin. The number below is the fast one
# discounted by nearly half, because the cost of guessing low is a
# surprise on the bill and the cost of guessing high is a slightly
# pessimistic quote.
WEIGHT_FETCH_BPS = 40_000_000


def baked_weights(image):
    """Which weight sets this image already carries inside itself."""
    name = (image or "").strip()
    for prefix in ("docker.io/", "index.docker.io/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    if not name:
        return ()
    for key, baked in IMAGE_BAKED_WEIGHTS.items():
        k = key
        for prefix in ("docker.io/", "index.docker.io/"):
            if k.startswith(prefix):
                k = k[len(prefix):]
                break
        if k == name:
            return baked
    return ()


def engines_for(image):
    """The engines an image is known to have, or None when we do not know.

    An unknown image gets the benefit of the doubt: the worker knows best,
    and it can be asked once the pod is up.

    The same image is written two ways - "bzz1/upscaler:x" and
    "docker.io/bzz1/upscaler:x" - and the registry does not care which one
    you use. The lookup does, so the prefix is taken off before the name
    is looked up.
    """
    name = (image or "").strip()
    for prefix in ("docker.io/", "index.docker.io/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    if not name:
        return None
    for key, engines in IMAGE_ENGINES.items():
        k = key
        for prefix in ("docker.io/", "index.docker.io/"):
            if k.startswith(prefix):
                k = k[len(prefix):]
                break
        if k == name:
            return engines
    return None


# What the AI pass does on the card, in frames per second. These are
# deliberately conservative: the NCNN build is slower than the TensorRT one
# the published benchmarks use, and the diffusion engines are an order of
# magnitude slower again. A wrong estimate in this direction costs you
# nothing; a wrong estimate in the other direction costs you money.
# Measured on an RTX 4090 (Runpod, driver 595, torch 2.8): the compact v3
# net did 300 frames of 636x480 in 20 seconds of model time, and the whole
# job - decode, model, encode - in 21.
#
# The net does all of its convolutions at the size of the input (only the
# last one and the pixel shuffle work at the output size), so what the
# price follows is the pixels of the input, not the frames. This number is
# measured, not borrowed.
MEASURED_MPX_PER_SEC = 4.5
# The card needs a moment to warm up before it is at full speed.
WARMUP_SECONDS = 2.0

# The deep RRDB net (the plain "x4plus", without the "-v3") does about
# twenty times the work of the compact net per pixel: 23 residual blocks
# against 32 convolutions. The number below is a guess, marked as one, and
# it is the reason the estimate says whether it is measured or not.
DEEP_MPX_PER_SEC = 0.25

# Which kind of net a model is. The compact family - the "-v3" models and
# the animevideo ones - is the fast path.
_COMPACT = ("v3", "animevideo", "ultramix", "ultrasharp", "remacri")


def model_class(model):
    """'compact' or 'deep' - the two kinds of Real-ESRGAN net."""
    m = (model or "").lower()
    if not m:
        return "compact"
    for token in _COMPACT:
        if token in m:
            return "compact"
    return "deep"

# Frames per second for the engines that have not been measured yet. These
# are guesses, and the UI says so.
GPU_FPS = {
    "realesrgan": 8.0,
    "seedvr2": 1.0,
    # STAR was measured on an L40S on 2026-11-01: a 320x180 clip of two
    # seconds, at 4x, took 600 s of inference - 0.083 frames a second
    # for a 1280x720 result. The number here is the same rate referred
    # to the 640x480 reference the scaling uses.
    "star": 0.25,
    "dloral": 0.145,
}

# The measured cost model for the diffusion engines.
#
# SeedVR2 was measured on an RTX 4090 on 2026-10-31, on the real film: the
# same 25 frames three ways, each timed twice so that the model load and
# the one-off costs cancel out.
#
#   1080p in, 1080p out   1.68 s a frame
#   1080p in,  480p out   0.96 s a frame
#   480p in,   480p out   0.68 s a frame
#
# Three numbers, and they say two things. The work follows the OUTPUT
# pixels - the transformer runs at the resolution it is producing - and it
# also follows the INPUT pixels, because the VAE encodes the frames it is
# given. On top of that there is a fixed cost per batch.
#
#   0.43 s a frame, plus 0.43 s per Mpx of output, plus 0.17 s per Mpx of input
#
# So a 480p restore fed with a 480p picture is two and a half times
# cheaper than the same restore fed with the 1080p original, and the
# native 1080p pass is two and a half times dearer than that. The old
# model followed one set of pixels only, and it made the 480p route look
# like a fifth of the 1080p one when it is closer to a half.
#
# The guess the estimate used before - 1.0 frame/s - was three times too
# slow, and it was the reason a 480p restore came out at the price of a
# 1080p one.
DIFFUSION_MODELS = {
    "seedvr2": {"fixed": 0.43, "out": 0.434, "in": 0.169},
    # DLoRAL was measured on an RTX 4090 on 2026-10-31: 1.14 s a frame
    # for 4x to 480p from a 120p canvas.
    #
    # It is a 4x model, so its input is a tiny canvas that scales with the
    # output, and the work follows the output: 0.41 Mpx in 1.14 s is
    # 0.36 Mpx/s. The model is written as a fixed part plus the output
    # pixels, which is what the one measurement can support; the input
    # term is left out because the canvas is a quarter of the output.
    #
    # So 1080p - which needs a card with 48 GB of VRAM, because the VAE
    # decoder's attention at that size wants 16 GB on its own - comes out
    # at about five times the 480p route.
    # DLoRAL, measured twice on an L40S:
    #   480p  (0.41 Mpx)   1.14 s a frame  (2026-10-31, a 120p canvas)
    #  1080p  (2.07 Mpx)  10.45 s a frame  (2026-11-02, a 480x270 canvas)
    #
    # The old model said 5.1 s at 1080p - it was fitted at 480p and scaled
    # the output linearly, and the cost does not scale that way: the
    # diffusion runs on the CANVAS (the target over four) and the attention
    # over it grows faster than the pixels do. The two measurements fit
    # 0.114 + 3.753 * Mpx^1.427.
    #
    # The VAE is not the culprit: the DLoRAL script enables its tiled
    # decoder by itself (src/DLoRAL_model.py calls _init_tiled_vae with
    # the --vae_decoder_tiled_size argument), so the decode is already
    # tiled. Tiling it harder would buy nothing.
    "dloral": {"fixed": 0.114, "out": 3.753, "out_pow": 1.427, "in": 0.0},
}

# The engines that have been measured on a card. The UI says so, next to
# the price.
MEASURED_FPS = {
    "seedvr2": 3.1,
    "dloral": 0.88,
    # STAR: 50 frames of a 320x180 clip at 4x in 600 s on an L40S -
    # 0.083 frames a second for a 1280x720 result. The number here is
    # that rate referred to the 640x480 reference the scaling uses,
    # which is what the table above is too.
    "star": 0.25,
}
GPU_FPS.update(MEASURED_FPS)

# The price of the card per hour, when the live catalogue cannot be reached.
# The secure price, which is what a pod usually gets.
# The pods this machine has running.
#
# A pod nobody is watching keeps billing by the hour, and the thing that
# watches them is this program. If it is killed - a restart, a crash - the
# finally that would have terminated the pod never runs, and the card
# keeps going until somebody notices. So the ids go in a file, and the next
# start terminates whatever is left in it.
POD_STATE = os.path.expanduser("~/.upscaler-gui-pods.json")


def _read_pod_state():
    try:
        with open(POD_STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _write_pod_state(state):
    try:
        with open(POD_STATE, "w") as fh:
            json.dump(state, fh, indent=2)
    except Exception:
        pass


def remember_pod(pod_id, image=""):
    """Write a pod down, so a crash cannot orphan it."""
    state = _read_pod_state()
    state[pod_id] = {"image": image, "since": time.time()}
    _write_pod_state(state)


def forget_pod(pod_id):
    state = _read_pod_state()
    if state.pop(pod_id, None) is not None:
        _write_pod_state(state)


FALLBACK_PRICES = {
    "NVIDIA GeForce RTX 4090": 0.89,
    "NVIDIA RTX A5000": 0.27,
    "NVIDIA L40S": 1.09,
    "NVIDIA A100 80GB PCIe": 1.79,
    "NVIDIA H100 PCIe": 2.90,
}

# Starting a pod: pulling the image and booting it. It is billed, and for a
# short clip it is most of the bill.
POD_OVERHEAD_SECONDS = 240


class RentalError(RuntimeError):
    pass


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class RunPod:
    """A Runpod pod that exists only while the job does."""

    provider = "runpod"

    def __init__(self, api_key, gpu="NVIDIA GeForce RTX 4090", image=None,
                 engine="realesrgan", region=None, disk=60, port=8000,
                 name="drm-upscale", log=print, keep_warm=False,
                 volume_id=None, workspace_size=None):
        self.api_key = api_key
        self.gpu = gpu
        # The diffusion engines live in images of their own. Getting this
        # wrong rents a card with the plain image, and the job fails on the
        # card with "the worker cannot run the star engine" - after the
        # money is spent and the image is pulled.
        self.image = image or (DEFAULT_IMAGE_DLORAL if engine == "dloral"
                               else DEFAULT_IMAGE_STAR if engine == "star"
                               else DEFAULT_IMAGE_SEEDVR2
                               if engine == "seedvr2"
                               else DEFAULT_IMAGE)
        self.region = region
        self.disk = disk
        self.port = port
        self.name = name
        self.log = log
        # Keep the card between jobs instead of throwing it away: the GPU is
        # released and the disk - with the weights on it - is kept, so the
        # next run of the same engine does not download them again. The
        # weights are eleven gigabytes and Hugging Face serves them at
        # 5-9 MB/s, which is twenty minutes of a card's time on every run.
        self.keep_warm = keep_warm
        # How long to wait between the tries when the data centre has no
        # card of the kind we want. A centre's cards come and go - the
        # volume's centre had an L40S at 10:50, none at 11:01 and one at
        # 11:04 - and a pod that cannot be created is not billed, so
        # waiting costs nothing but the delay.
        self.retry_wait = float(os.environ.get("UPSCALER_RENT_RETRY_WAIT", "30"))
        # A network volume keeps the weights even if the pod is terminated,
        # and shares them between pods. Set RUNPOD_VOLUME_ID to "" to turn it
        # off (the weights are then fetched onto the pod's own disk).
        self.volume_id = (volume_id if volume_id is not None
                          else os.environ.get("RUNPOD_VOLUME_ID",
                                              DEFAULT_VOLUME_ID))
        # The container disk at /workspace is what the pod has: it
        # survives a STOP and is wiped when the pod is TERMINATED. So a
        # card that is kept warm between jobs still has the weights on it
        # the next morning, and a card that is thrown away does not.
        #
        # There is no separate per-pod volume disk: one of those is billed
        # at $0.20 a GB a month for as long as the pod exists, parked or
        # not, and it would be pure waste on a card that is about to be
        # thrown away.
        self.workspace_size = int(workspace_size or
                                  os.environ.get("RUNPOD_WORKSPACE_GB", "60"))
        self.pod_id = None
        self.endpoint = None
        self.cost_per_hour = None
        # When the card started billing and when it stopped, by this
        # machine's clock. The session spending cap is settled against
        # these, not against the estimate: an estimate is a guess, and a
        # cap that only ever sees guesses is not a cap.
        self.billing_since = None
        self.billing_until = None
        # True while a stopped card is being woken up: it stays EXITED
        # until it has booted, and that is not a failure.
        self.starting = False
        # Nothing is rented until somebody says so.
        self.approved = False
        self.estimate = None

    # --- the api ----------------------------------------------------------

    def _request(self, method, path, body=None):
        url = f"{RUNPOD_API}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise RentalError(f"Runpod said {e.code}: {detail}")

    # --- the lifecycle ----------------------------------------------------

    def approve(self, estimate=None):
        """The user has seen the estimate and said yes."""
        self.approved = True
        self.estimate = estimate
        return self

    def price_per_hour(self):
        """What this card costs right now, if the catalogue can be reached."""
        if self.cost_per_hour:
            return float(self.cost_per_hour)
        try:
            data = self._request("GET", "/catalog/gpus")
            for g in data.get("gpus", []):
                if g.get("id") == self.gpu:
                    p = g.get("price") or {}
                    # The secure price is what a pod normally gets; the
                    # community price is the spot one. Take the dearer.
                    prices = [float(p[k]) for k in ("secure", "community")
                              if p.get(k)]
                    if prices:
                        return max(prices)
        except Exception:
            pass
        return FALLBACK_PRICES.get(self.gpu, 0.89)

    def plan(self, frames=0, fps=25.0, engine="realesrgan", width=640, height=480,
             model=None, out_width=None, out_height=None):
        """What this job would cost, and what a batch of them would cost.

        Nothing here spends anything.
        """
        price = self.price_per_hour()
        width = int(width or 640)
        height = int(height or 480)
        pixels = max(1, width * height)
        out_mpx = None
        per_frame = None
        if engine == "realesrgan":
            # The cost follows the pixels of the input, and the pixels per
            # second depend on WHICH net is doing the work: the compact one
            # (the "-v3" family and the animevideo models) is measured, the
            # deep RRDB one (the plain "x4plus") is a good deal slower per
            # pixel. Quoting the compact's rate for the deep net is how an
            # estimate becomes a lie.
            mpx = MEASURED_MPX_PER_SEC if model_class(model) == "compact" \
                else DEEP_MPX_PER_SEC
            ai_seconds = (frames * pixels / (mpx * 1e6)
                          + WARMUP_SECONDS) if frames else 0.0
            rate = mpx * 1e6 / pixels
            measured = model_class(model) == "compact"
        else:
            # A diffusion engine works at the resolution it is PRODUCING, so
            # the cost follows the OUTPUT pixels - not the input ones. The
            # old estimate priced it by the input, which is how a 480p
            # restore came out at the price of a 1080p one.
            #
            # It also encodes the frames it is given, so the input costs
            # something too, and the measured model has both terms.
            ow = int(out_width or width)
            oh = int(out_height or height)
            out_mpx = max(1, ow * oh) / 1e6
            in_mpx = pixels / 1e6
            dmodel = DIFFUSION_MODELS.get(engine)
            if dmodel:
                per_frame = (dmodel["fixed"]
                             + dmodel["out"] * out_mpx ** dmodel.get("out_pow", 1.0)
                             + dmodel["in"] * in_mpx)
                ai_seconds = frames * per_frame if frames else 0.0
                rate = 1.0 / per_frame if per_frame else 0.0
            else:
                # a guess, scaled by the output the way the measured one is
                base = GPU_FPS.get(engine, GPU_FPS["realesrgan"])
                rate = base * (640 * 480) / max(1, ow * oh)
                per_frame = 1.0 / rate if rate else None
                ai_seconds = (frames / rate) if frames else 0.0
            measured = engine in MEASURED_FPS
        one = ai_seconds + POD_OVERHEAD_SECONDS
        # The cold start.
        #
        # A diffusion engine needs its weights, and they are big: eleven
        # gigabytes for DLoRAL, seven and a half for STAR. An image that
        # carries them pays for them in the pull - the pull is at fifty
        # megabytes a second - but an image that does not have them makes
        # the card fetch them from Hugging Face at five to nine megabytes a
        # second. That is twenty-five minutes of a card's time, and it
        # happens on EVERY run that lands on a card without them - which is
        # every run, unless the network volume has them already.
        #
        # An estimate that does not say so quotes $0.08 for a job that
        # costs $0.58, and the difference arrives as a surprise on the
        # bill. So the fetch is a line of its own, and the first-run total
        # is the number the user approves.
        # The height of the RESULT. The diffusion engines work at the
        # size they are producing, and everything else works at the size
        # of the source.
        result_h = int(out_height or height or 1080)
        weights_bytes = 0
        if engine in WEIGHTS_BYTES and engine not in baked_weights(self.image):
            weights_bytes = WEIGHTS_BYTES[engine]
        fetch_seconds = weights_bytes / WEIGHT_FETCH_BPS if weights_bytes else 0.0
        rows = []
        for n in (1, 5, 10, 25):
            # One pod session for n clips: the spin-up is paid once.
            total = ai_seconds * n + POD_OVERHEAD_SECONDS
            rows.append({"clips": n,
                         "seconds": round(total),
                         "cost": round(total / 3600.0 * price, 4)})
        return {
            "gpu": self.gpu,
            "price_per_hour": price,
            "frames": frames,
            "fps": fps,
            "seconds_of_video": round(frames / fps, 1) if fps else 0,
            "width": width,
            "height": height,
            "fps_on_the_card": round(rate, 1),
            # The pixels a second the card gets through. For Real-ESRGAN
            # that is the net's own rate; for a diffusion it is the rate
            # the measured model works at, which is a different thing (the
            # diffusion also encodes the frames it is given).
            "mpx_per_second": (round(out_mpx / per_frame, 2)
                               if (out_mpx and per_frame)
                               else (MEASURED_MPX_PER_SEC if measured else None)),
            "measured": measured,
            "ai_seconds": round(ai_seconds),
            "spin_up_seconds": POD_OVERHEAD_SECONDS,
            "one_clip_seconds": round(one),
            "one_clip_cost": round(one / 3600.0 * price, 4),
            # The weights a cold card has to fetch before it can work, and
            # what that costs. Zero when the image carries them.
            "weights_mb": round(weights_bytes / 1e6),
            # How much memory the card has to have for this result. A card
            # with less does not fail at the start: it fails at the end,
            # after the money is spent.
            "vram_gb": vram_for(engine, result_h),
            "fetch_seconds": round(fetch_seconds),
            "fetch_cost": round(fetch_seconds / 3600.0 * price, 4),
            "first_run_seconds": round(one + fetch_seconds),
            "first_run_cost": round((one + fetch_seconds) / 3600.0 * price, 4),
            "batches": rows,
            "image": self.image,
            "engine": engine,
        }

    def create(self):
        if not self.approved:
            raise RentalError(
                "this pod has not been approved. Estimate the cost first, "
                "then approve it.")
        # A card that was kept warm from the last job is the one to use: it
        # already has the weights on its disk, so the run does not download
        # them again.
        warm = self.find_warm()
        if warm:
            self.pod_id = warm.get("id")
            self.cost_per_hour = warm.get("cost")
            self.log(f"the card from the last job is still here "
                     f"({self.pod_id}) - waking it up instead of downloading "
                     f"the weights again")
            self.starting = True
            self.billing_since = time.time()
            try:
                self._request("POST", f"/pods/{self.pod_id}/action",
                              {"action": "start"})
            except Exception as e:
                # The machine that card sits on may have given its GPU to
                # somebody else in the meantime. That is not the end of the
                # job: a new card is rented instead.
                self.starting = False
                self.log(f"could not wake that card up ({str(e)[:120]}) - "
                         f"renting a new one")
                self.pod_id = None
                warm = None
        if not warm:
            body = {
                "name": self.name,
                "image": self.image,
                "gpu": {"id": self.gpu, "count": 1},
                "disk": self.disk,
                "ports": [f"{self.port}/http"],
                # Global networking gives the pod a real public IP. Without it
                # the pod sits on a CGNAT address and the only way in is
                # Runpod's proxy, which sits behind Cloudflare: that caps the
                # request at 100 seconds (524 on anything longer) and can mangle
                # an uploaded body.
                "globalNetworking": True,
            }
            # The network volume, when there is one, replaces the pod's own
            # volume disk. That is worth doing: a pod's own volume disk is
            # billed at $0.20 a GB a month while the pod is parked, and a
            # parked pod that keeps its weights is the whole point of keeping
            # the card. With the volume there is nothing per-pod to pay for.
            if self.volume_id:
                body["mounts"] = {"network": [{"volumeId": self.volume_id,
                                              "path": "/workspace"}]}
                # A pod can only mount a volume from its own data centre, so
                # the volume decides where the pod goes.
                dc = self.volume_datacenter()
                if dc:
                    body["dataCenterIds"] = [dc]
                    self.log(f"the weights live on a volume in {dc} - the card "
                             f"goes there too, because a pod can only mount a "
                             f"volume from its own data centre")
            elif self.region:
                body["dataCenterIds"] = [self.region]
        self.log(f"asking Runpod for a {self.gpu}…")
        # A data centre's cards come and go. US-TX-3 had an L40S at 10:50
        # and none at 11:01 and one at 11:04 - so a single refusal to rent
        # is not a fact about the centre, it is a fact about that minute.
        # A pod that cannot be created is not billed, so trying again costs
        # nothing but a few seconds of waiting.
        # Every rental gets the retries, not only the ones with a volume:
        # a pool of cards empties and fills, and a pod that cannot be
        # created is not billed, so trying again costs nothing but the
        # wait.
        tries = 10
        pod = None
        last_error = None
        for attempt in range(tries):
            try:
                pod = self._request("POST", "/pods", body)
                last_error = None
                break
            except Exception as err:
                last_error = err
                if "instances available" not in str(err):
                    raise
                if attempt + 1 < tries:
                    self.log(f"no {self.gpu} in that data centre at the "
                             f"moment - trying again ({attempt + 2}/{tries})")
                    time.sleep(self.retry_wait)
        if pod is None:
            # A pod can only mount a volume from its own data centre, so the
            # volume narrows the choice of card to that one centre. If there
            # is nothing of the kind we want there, the job is better off
            # without the volume: the weights are fetched onto the pod's own
            # disk instead, which is slower but works.
            if not self.volume_id:
                raise last_error
            self.log("there is no card of that kind where the volume is - "
                     "going without it, and the weights are fetched onto "
                     "the card's own disk instead")
            body.pop("mounts", None)
            body.pop("dataCenterIds", None)
            pod = self._request("POST", "/pods", body)
        self.pod_id = pod.get("id")
        self.cost_per_hour = pod.get("cost")
        if not self.pod_id:
            raise RentalError(f"Runpod did not give us a pod: {pod}")
        self.billing_since = time.time()
        remember_pod(self.pod_id, self.image)
        self.log(f"pod {self.pod_id} "
                 + (f"at ${self.cost_per_hour}/h" if self.cost_per_hour else ""))
        return self

    def volume_datacenter(self):
        """Where the volume lives - and so where the pod has to go."""
        if not self.volume_id:
            return None
        try:
            vols = self._request("GET", "/network-volumes")
            if isinstance(vols, dict):
                vols = vols.get("networkVolumes") or []
            for v in vols or []:
                if v.get("id") == self.volume_id:
                    return v.get("dataCenter")
        except Exception:
            pass
        return None

    def find_warm(self):
        """A stopped card with the same image and GPU, if there is one.

        Stopped, not terminated: the disk is still there, with the weights
        on it, and the GPU is not billed while it sits.
        """
        if not self.keep_warm:
            return None
        try:
            pods = self._request("GET", "/pods").get("pods", [])
        except Exception:
            return None
        for p in pods:
            if p.get("status") != "EXITED":
                continue
            if p.get("image") != self.image:
                continue
            if (p.get("gpu") or {}).get("id") != self.gpu:
                continue
            return p
        return None

    def warm_pods(self):
        """The cards this account is keeping warm."""
        try:
            pods = self._request("GET", "/pods").get("pods", [])
        except Exception:
            return []
        return [p for p in pods if p.get("status") == "EXITED"]

    def wait_ready(self, timeout=900):
        """Poll until the container is up and the worker answers."""
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            pod = self._request("GET", f"/pods/{self.pod_id}")
            status = pod.get("status")
            if status != last:
                self.log(f"pod {self.pod_id}: {status}")
                last = status
            if status == "RUNNING":
                url = self._endpoint_from(pod)
                if url:
                    self.endpoint = url
                    if self._ping(url):
                        self.starting = False
                        self.log(f"the worker is up at {url}")
                        return url
            if status in ("ERROR", "TERMINATED"):
                raise RentalError(f"the pod went {status} before it was ready")
            if status == "EXITED" and not self.starting:
                # A pod that is being woken up from a stop stays EXITED
                # until it has actually booted, so that is not a failure
                # while it is starting.
                raise RentalError(f"the pod went {status} before it was ready")
            time.sleep(5)
        raise RentalError(f"the pod was still not ready after {timeout//60} minutes")

    def _endpoint_from(self, pod):
        """Where the worker is actually reachable.

        A pod often sits on a CGNAT address (100.64.0.0/10), which is not
        routable from outside. Those are reached through Runpod's proxy,
        and the proxy is addressed by the PRIVATE port - not the public one.
        """
        runtime = pod.get("runtime") or {}
        for p in runtime.get("ports") or []:
            if str(p.get("private")) != str(self.port):
                continue
            ip = p.get("ip") or ""
            if not ip:
                continue
            if ip.startswith("100.") or ip.startswith("10."):
                # Not a public address: the proxy is the way in.
                return f"https://{self.pod_id}-{self.port}.proxy.runpod.net"
            if p.get("public"):
                return f"http://{ip}:{p['public']}"
        return None

    def _ping(self, url, tries=6):
        # Cloudflare checks the fingerprint of the client, and the default
        # Python one does not pass. The same header cloud.py sends.
        last = ""
        for _ in range(tries):
            try:
                req = urllib.request.Request(url.rstrip("/") + "/health",
                                             headers={"User-Agent": BROWSER_UA})
                with urllib.request.urlopen(req, timeout=15) as r:
                    if r.status == 200:
                        return True
                    last = f"HTTP {r.status}"
            except Exception as e:
                last = str(e)
            time.sleep(5)
        self.log(f"the worker did not answer at {url} ({last})")
        return False

    def stop(self):
        """Release the GPU but keep the card, with its disk.

        Stopping is not terminating: the volume disk at /workspace stays,
        so the weights are still there for the next run, and the GPU is not
        billed while the card sits. It still costs the volume disk
        ($0.20 per GB per month) until it is terminated.
        """
        if not self.pod_id:
            return
        try:
            self._request("POST", f"/pods/{self.pod_id}/action",
                          {"action": "stop"})
            self.log(f"pod {self.pod_id} stopped - the GPU is free, the disk "
                     f"and the weights stay on it")
        except Exception as e:
            self.log(f"WARNING: could not stop pod {self.pod_id}: {e}")
        finally:
            self.billing_until = time.time()
            self.pod_id = None

    def _pod_gone(self, pod_id):
        """Has Runpod really let this pod go?

        A DELETE can answer 200 and leave the pod running and billing, so
        the answer to the DELETE is not the answer. The pod list is.
        """
        try:
            pod = self._request("GET", f"/pods/{pod_id}")
        except RentalError as e:
            # 404: there is no such pod any more.
            return " 404" in str(e) or "not found" in str(e).lower()
        except Exception:
            return False
        if not pod or not pod.get("id"):
            return True
        return pod.get("status") in ("TERMINATED",) or pod.get("desiredStatus") == "TERMINATED"

    def terminate(self, verify_tries=None, verify_wait=None):
        """Throw the card away: the GPU, the disk and everything on it.

        The DELETE is checked against GET /pods/{id} afterwards and repeated
        if the pod is still there. A pod that will not go stays in the pod
        file, so the next start sweeps it again, and the log says so loudly.
        """
        if not self.pod_id:
            return
        pid = self.pod_id
        tries = int(verify_tries if verify_tries is not None
                    else os.environ.get("UPSCALER_TERMINATE_TRIES", "4"))
        wait = float(verify_wait if verify_wait is not None
                     else os.environ.get("UPSCALER_TERMINATE_WAIT", "5"))
        gone = False
        try:
            for attempt in range(max(1, tries)):
                try:
                    self._request("DELETE", f"/pods/{pid}")
                except Exception as e:
                    if " 404" not in str(e):
                        self.log(f"the DELETE for pod {pid} failed: {str(e)[:120]}")
                if wait:
                    time.sleep(wait)
                if self._pod_gone(pid):
                    gone = True
                    break
                self.log(f"pod {pid} is still there after the DELETE - "
                         f"asking again ({attempt + 2}/{tries})")
        finally:
            self.billing_until = time.time()
            self.pod_id = None
        if gone:
            forget_pod(pid)
            self.log(f"pod {pid} terminated - you stop paying for it now")
        else:
            self.log(f"WARNING: pod {pid} may still be running and billing. "
                     f"It stays in {POD_STATE}, so the next start sweeps it; "
                     f"to end it now: curl -X DELETE -H 'Authorization: Bearer <key>' "
                     f"{RUNPOD_API}/pods/{pid}")
        return gone

    def cost_so_far(self):
        """What this rental has cost up to now, by this machine's clock.

        The card bills from the moment it is created (the image pull is
        billed) until it is let go; 30 seconds are added for the API calls
        at either end, so the figure errs high.
        """
        if not self.billing_since:
            return 0.0
        end = self.billing_until or time.time()
        price = float(self.cost_per_hour or self.price_per_hour() or 0)
        return (end - self.billing_since + 30.0) / 3600.0 * price

    def sweep_orphans(self):
        """Terminate the pods this machine created and nobody is watching.

        Called when the program starts: a pod whose job died with the
        process is still billing, and nothing else will ever look at it.
        """
        state = _read_pod_state()
        gone = []
        for pid in list(state):
            try:
                pod = self._request("GET", "/pods/" + pid)
            except Exception:
                forget_pod(pid)
                continue
            if pod.get("status") in ("TERMINATED", "EXITED"):
                forget_pod(pid)
                continue
            self.log(f"a pod from an earlier run is still up ({pid}) - "
                     f"terminating it, it would keep billing")
            try:
                self._request("DELETE", "/pods/" + pid)
            except Exception as e:
                self.log(f"  could not terminate it: {str(e)[:120]}")
                continue
            forget_pod(pid)
            gone.append(pid)
        return gone

    def destroy(self):
        """Give the card back.

        With keep_warm the card is stopped, not terminated: the GPU is
        released and the disk - with the weights on it - is kept for the
        next run. Without it, the card is thrown away.
        """
        if self.keep_warm:
            # A stopped card is not billed for its GPU and is not an orphan:
            # it is kept on purpose, so it leaves the sweep's list.
            pid = self.pod_id
            self.stop()
            if pid:
                forget_pod(pid)
        else:
            # terminate() forgets the pod only once Runpod has really let it
            # go. (It used to forget it after pod_id had already been set to
            # None, so the file was never cleaned and the check was moot.)
            self.terminate()


class Local:
    """The worker as a local process.

    Not a rental - but it goes through exactly the same create/wait/destroy
    path, so the flow can be exercised without an account, and the
    "local" backend keeps working when nothing is rented.
    """

    provider = "local"

    def __init__(self, engine="realesrgan", port=None, log=print, **kw):
        self.engine = engine
        self.port = port or _free_port()
        self.log = log
        self.proc = None
        self.endpoint = None
        self.pod_id = None
        self.cost_per_hour = 0.0

    def cost_so_far(self):
        return 0.0

    def create(self):
        env = dict(os.environ)
        env.setdefault("UPSCALER_BIN",
                       os.path.expanduser("~/upscaler-gui/vendor/upscaler/bin/mac-amd64"))
        env.setdefault("UPSCALER_MODELS",
                       os.path.expanduser("~/upscaler-gui/vendor/upscaler/models"))
        env["PORT"] = str(self.port)
        self.log(f"starting the worker locally on port {self.port}…")
        self.proc = subprocess.Popen([ "python3", WORKER], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.pod_id = f"local-{self.port}"
        return self

    def wait_ready(self, timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/health", timeout=5) as r:
                    if r.status == 200:
                        self.endpoint = f"http://127.0.0.1:{self.port}"
                        self.log(f"the worker is up at {self.endpoint}")
                        return self.endpoint
            except Exception:
                pass
            time.sleep(1)
        raise RentalError("the local worker did not come up")

    def destroy(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
            self.log("the local worker is stopped")


def make(provider="runpod", engine="realesrgan", api_key=None, gpu=None,
         image=None, region=None, log=print, **kw):
    if provider == "local":
        return Local(engine=engine, log=log, **kw)
    if provider != "runpod":
        raise RentalError(f"unknown rental provider: {provider}")
    if not api_key:
        raise RentalError("no Runpod API key - put it in the GUI")
    choices = GPU_CHOICES.get(engine) or GPU_CHOICES["realesrgan"]
    if not gpu:
        gpu = choices[0]["id"]
    return RunPod(api_key, gpu=gpu, image=image, engine=engine,
                  region=region, log=log, **kw)
