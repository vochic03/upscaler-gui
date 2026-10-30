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

# What to ask for, per engine. DLoRAL needs 24 GB+ of VRAM; Real-ESRGAN
# is happy with much less.
GPU_CHOICES = {
    "realesrgan": [
        {"id": "NVIDIA GeForce RTX 4090", "label": "RTX 4090 24 GB — $0.44/h"},
        {"id": "NVIDIA GeForce RTX A5000", "label": "RTX A5000 24 GB — $0.44/h"},
        {"id": "NVIDIA L40S", "label": "L40S 48 GB — $0.99/h"},
        {"id": "NVIDIA A100 PCIe", "label": "A100 80 GB — $1.19/h"},
    ],
    "dloral": [
        {"id": "NVIDIA L40S", "label": "L40S 48 GB — $0.99/h"},
        {"id": "NVIDIA A100 PCIe", "label": "A100 80 GB — $1.19/h"},
        {"id": "NVIDIA H100 PCIe", "label": "H100 80 GB — $2.90/h"},
    ],
}

# The container that has the worker in it. It has to be pushed to a
# registry the rental service can pull from.
DEFAULT_IMAGE = os.environ.get("UPSCALER_IMAGE", "docker.io/bzz1/upscaler:latest")
DEFAULT_IMAGE_DLORAL = os.environ.get("UPSCALER_IMAGE_DLORAL",
                                      "docker.io/bzz1/upscaler:latest")


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

# Frames per second for the engines that have not been measured yet. These
# are guesses, and the UI says so.
GPU_FPS = {
    "realesrgan": 8.0,
    "seedvr2": 1.0,
    "star": 0.15,
    "dloral": 0.145,
}

# The price of the card per hour, when the live catalogue cannot be reached.
# The secure price, which is what a pod usually gets.
FALLBACK_PRICES = {
    "NVIDIA GeForce RTX 4090": 0.89,
    "NVIDIA GeForce RTX A5000": 0.44,
    "NVIDIA L40S": 0.99,
    "NVIDIA A100 PCIe": 1.19,
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
                 name="drm-upscale", log=print):
        self.api_key = api_key
        self.gpu = gpu
        self.image = image or (DEFAULT_IMAGE_DLORAL if engine == "dloral"
                               else DEFAULT_IMAGE)
        self.region = region
        self.disk = disk
        self.port = port
        self.name = name
        self.log = log
        self.pod_id = None
        self.endpoint = None
        self.cost_per_hour = None
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

    def plan(self, frames=0, fps=25.0, engine="realesrgan", width=640, height=480):
        """What this job would cost, and what a batch of them would cost.

        Nothing here spends anything.
        """
        price = self.price_per_hour()
        width = int(width or 640)
        height = int(height or 480)
        pixels = max(1, width * height)
        if engine == "realesrgan":
            # measured: the cost follows the pixels of the input
            ai_seconds = (frames * pixels / (MEASURED_MPX_PER_SEC * 1e6)
                          + WARMUP_SECONDS) if frames else 0.0
            rate = MEASURED_MPX_PER_SEC * 1e6 / pixels
            measured = True
        else:
            # a guess, scaled by the resolution the way the measured one is
            base = GPU_FPS.get(engine, GPU_FPS["realesrgan"])
            rate = base * (640 * 480) / pixels
            ai_seconds = (frames / rate) if frames else 0.0
            measured = False
        one = ai_seconds + POD_OVERHEAD_SECONDS
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
            "mpx_per_second": MEASURED_MPX_PER_SEC if measured else None,
            "measured": measured,
            "ai_seconds": round(ai_seconds),
            "spin_up_seconds": POD_OVERHEAD_SECONDS,
            "one_clip_seconds": round(one),
            "one_clip_cost": round(one / 3600.0 * price, 4),
            "batches": rows,
            "image": self.image,
            "engine": engine,
        }

    def create(self):
        if not self.approved:
            raise RentalError(
                "this pod has not been approved. Estimate the cost first, "
                "then approve it.")
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
        if self.region:
            body["dataCenterIds"] = [self.region]
        self.log(f"asking Runpod for a {self.gpu}…")
        pod = self._request("POST", "/pods", body)
        self.pod_id = pod.get("id")
        self.cost_per_hour = pod.get("cost")
        if not self.pod_id:
            raise RentalError(f"Runpod did not give us a pod: {pod}")
        self.log(f"pod {self.pod_id} "
                 + (f"at ${self.cost_per_hour}/h" if self.cost_per_hour else ""))
        return self

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
                        self.log(f"the worker is up at {url}")
                        return url
            if status in ("ERROR", "EXITED", "TERMINATED"):
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

    def destroy(self):
        if not self.pod_id:
            return
        try:
            self._request("DELETE", f"/pods/{self.pod_id}")
            self.log(f"pod {self.pod_id} terminated - you stop paying for it now")
        except Exception as e:
            # A failure here means the pod may still be running and billing.
            # Say so loudly.
            self.log(f"WARNING: could not terminate pod {self.pod_id}: {e}")
            self.log(f"terminate it by hand: "
                     f"curl -X DELETE -H 'Authorization: Bearer <key>' "
                     f"{RUNPOD_API}/pods/{self.pod_id}")
        finally:
            self.pod_id = None


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
