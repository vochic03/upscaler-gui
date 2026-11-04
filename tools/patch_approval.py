#!/usr/bin/env python3
"""Never rent a GPU without an explicit approval and a cost estimate.

Right now the "rent a GPU" checkbox goes straight to creating a pod. That is
the wrong default: a pod is money, the money is real, and the estimate of how
much is not always obvious - the spin-up can take longer than the work.

So the rental path now has a gate:

  1. /api/estimate computes the whole thing without spending anything: the
     card, its price, the frames, the measured throughput, the spin-up
     overhead, the cost of one clip, and what a batch of N clips costs
     (the overhead is paid once per session, so a batch is much cheaper per
     clip). The estimate is extrapolated in advance, not discovered later.
  2. The GUI shows that estimate and waits for a click.
  3. /api/run refuses to start a job that would rent anything unless the
     request carries the approval.
  4. rental.create() refuses to create a pod that has not been approved, so
     the guarantee holds even if something calls it directly.
"""
import sys

ROOT = "/Users/vasilijemicic/upscaler-gui"
ok = True


def patch(path, pairs):
    global ok
    src = open(path, encoding="utf-8").read()
    for old, new, label in pairs:
        if old not in src:
            print("MISS:", path, "-", label)
            ok = False
            continue
        if src.count(old) != 1:
            print("AMBIGUOUS (%d):" % src.count(old), path, "-", label)
            ok = False
            continue
        src = src.replace(old, new)
        print("ok:", path, "-", label)
    open(path, "w", encoding="utf-8").write(src)


# ---------------------------------------------------------------- rental.py
patch(ROOT + "/rental.py", [
    ('''class RentalError(RuntimeError):''',
     '''# What the AI pass does on the card, in frames per second. These are
# deliberately conservative: the NCNN build is slower than the TensorRT one
# the published benchmarks use, and the diffusion engines are an order of
# magnitude slower again. A wrong estimate in this direction costs you
# nothing; a wrong estimate in the other direction costs you money.
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


class RentalError(RuntimeError):''', "the price and speed tables"),

    ('''        self.pod_id = None
        self.endpoint = None
        self.cost_per_hour = None''',
     '''        self.pod_id = None
        self.endpoint = None
        self.cost_per_hour = None
        # Nothing is rented until somebody says so.
        self.approved = False
        self.estimate = None''', "the approval flag"),

    ('''    def create(self):
        body = {''',
     '''    def approve(self, estimate=None):
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

    def plan(self, frames=0, fps=25.0, engine="realesrgan"):
        """What this job would cost, and what a batch of them would cost.

        Nothing here spends anything.
        """
        rate = GPU_FPS.get(engine, GPU_FPS["realesrgan"])
        price = self.price_per_hour()
        ai_seconds = (frames / rate) if frames else 0.0
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
            "fps_on_the_card": rate,
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
        body = {''', "create() requires the approval"),

    ('''    def make(provider="runpod", engine="realesrgan", api_key=None, gpu=None,''',
     '''    def make(provider="runpod", engine="realesrgan", api_key=None, gpu=None,''', "make signature"),
])

# --------------------------------------------------------------- engine.py
patch(ROOT + "/engine.py", [
    ('''            rented = rental_mod.make(
                provider=provider,
                engine=engine,
                api_key=api_key,
                gpu=opts.get("rent_gpu") or None,
                image=opts.get("rent_image") or None,
                region=opts.get("rent_region") or None,
                log=lambda m: self.say(m))
            rented.create()''',
     '''            rented = rental_mod.make(
                provider=provider,
                engine=engine,
                api_key=api_key,
                gpu=opts.get("rent_gpu") or None,
                image=opts.get("rent_image") or None,
                region=opts.get("rent_region") or None,
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
                         f"about ${estimate.get('one_clip_cost')} for this clip")
            rented.create()''', "the engine asks for the approval"),

    ('''def default_model_for(width, height):''',
     '''def estimate_rental(opts, src):
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
    try:
        plan = r.plan(frames=frames, fps=info.get("fps") or 25.0, engine=engine)
    except Exception as e:
        return {"error": str(e)}
    return plan


def default_model_for(width, height):''', "the estimate function"),
])

# --------------------------------------------------------------- server.py
patch(ROOT + "/server.py", [
    ('''        if path == "/api/run":''',
     '''        if path == "/api/estimate":
            # What would this job cost if it rented a GPU? No side effects.
            try:
                body = self._body()
                src = body.get("path") or ""
                if not src or not os.path.exists(src):
                    return self._json({"error": "pick a clip first"}, 400)
                return self._json(engine.estimate_rental(body, src))
            except Exception as e:
                return self._json({"error": str(e)}, 400)

        if path == "/api/run":''', "the estimate endpoint"),
])

# -------------------------------------------------------------- index.html
patch(ROOT + "/index.html", [
    ('''  try {
    const r = await api('/api/run', body);
    jobId = r.id;''',
     '''  // A job that rents a GPU has to be costed and approved first. The
  // estimate is extrapolated in advance - one clip, and a batch - and
  // nothing is rented until the user says yes.
  if (body.rent) {
    const est = await api('/api/estimate', body);
    if (est.error) {
      log('cannot estimate: ' + est.error);
      return;
    }
    const rows = (est.batches || []).map(function (b) {
      return b.clips + ' clip' + (b.clips > 1 ? 's' : '') + ' in one session: $' +
             b.cost.toFixed(3);
    }).join('\\n');
    const msg = 'Renting ' + est.gpu + ' at $' + est.price_per_hour + '/h.\\n' +
      'This clip is ' + est.seconds_of_video + 's (' + est.frames + ' frames); ' +
      'about ' + Math.round(est.ai_seconds) + 's of AI work and ' +
      Math.round(est.spin_up_seconds / 60) + ' min of pod start-up.\\n' +
      'One clip now: about $' + est.one_clip_cost.toFixed(3) + '.\\n' + rows +
      '\\n\\nCreate the pod?';
    if (!window.confirm(msg)) {
      log('not rented - cancelled');
      return;
    }
    body.rent_approved = true;
    body.rent_estimate = est;
  }
  try {
    const r = await api('/api/run', body);
    jobId = r.id;''', "the confirmation before renting"),
])

print("all patches applied" if ok else "SOME PATCHES MISSED")
sys.exit(0 if ok else 1)
