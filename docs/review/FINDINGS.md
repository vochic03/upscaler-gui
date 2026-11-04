# Findings: measured, left undecided

Everything here was measured on the real material, not guessed.

> **Status (review, 2026-11-04):** all four have since been acted on - see
> `REVIEW-NOTES.md` for what changed, what was rejected and why. The text
> below is kept as it was written.

**Nothing in this file has been changed in the code.** These are deductions
I did not act on. How the picture should look is not my call - it is the
user's, or Claude's. What follows is the measurement, the reasoning, and a
ready-to-apply patch, so the decision can be made on evidence instead of on
a hunch.

---

## 1. The SeedVR2 guide is a quarter of the target

**Where:** `engine.py`, the `else` branch of the pre-scale decision - the
one that runs for every engine except DLoRAL.

**What the code does.** The guide size comes from the model's *native scale*:

```python
pre = min(target[0] / (src_w * scale), target[1] / (src_h * scale))
pre_w = even(max(2, round(src_w * pre)))
```

For Real-ESRGAN that is right: the model multiplies by its factor, so the
guide has to be that much smaller or the result lands four times too big.

For **SeedVR2 it is wrong**, because SeedVR2 does not enlarge - it
*restores*. `worker/diffusion_job.py` runs it as:

```
python3 inference_cli.py <in> --output <out> --resolution <short side> ...
```

The model generates the result *at* that resolution. The factor belongs to
the output, not to the guide. The `else` branch cannot tell the two apart:
`native_scale_for(model)` returns 4 for anything it does not recognise,
and the cloud path is entered with no Real-ESRGAN model at all.

**What it costs.** Source 640x480, target 1440x1080, `scale = 4` gives
`pre = 0.5625`, so the guide is **360x270** - a quarter of the source's
pixel count and a sixteenth of the target's. The model is asked to invent
1440x1080 out of a picture smaller than the original. It is visible in the
job directory: both `staged.mp4` and `upload.mp4` are 360x270.

**What it should be.** The guide goes **up** to the target, lanczos. The
model then conditions on the whole frame, and whatever it adds is added on
top of the picture instead of on top of four times less picture. The cost
follows the output resolution, which does not change - so the extra is
upload bytes and a slightly larger VAE encode, not a slower model.

**Ready to apply.** This is a suggestion, not a change:

```diff
         else:
             scale = native_scale_for(model)
             if target:
-                pre = min(target[0] / (src_info["width"] * scale),
-                          target[1] / (src_info["height"] * scale))
-                pre_w = even(max(2, round(src_info["width"] * pre)))
-                pre_h = even(max(2, round(src_info["height"] * pre)))
-                extra.append(f"scale={pre_w}:{pre_h}:flags=lanczos")
-                self.say(f"the AI runs at its native {scale}x on a "
-                         f"{pre_w}x{pre_h} frame")
+                if engine == "seedvr2":
+                    # SeedVR2 restores: it is asked for the target and it
+                    # generates the result at that size. It is NOT a 4x
+                    # enlargement, so the guide must not be scaled DOWN to
+                    # a quarter of the target - that throws away three
+                    # quarters of the real detail and leaves the model to
+                    # invent it.
+                    pre = min(target[0] / src_info["width"],
+                              target[1] / src_info["height"])
+                    pre_w = even(max(2, round(src_info["width"] * pre)))
+                    pre_h = even(max(2, round(src_info["height"] * pre)))
+                    extra.append(f"scale={pre_w}:{pre_h}:flags=lanczos")
+                    self.say(f"the guide goes up to {pre_w}x{pre_h}: "
+                             f"SeedVR2 restores to the target, it does not "
+                             f"enlarge, so the guide keeps its detail")
+                else:
+                    pre = min(target[0] / (src_info["width"] * scale),
+                              target[1] / (src_info["height"] * scale))
+                    pre_w = even(max(2, round(src_info["width"] * pre)))
+                    pre_h = even(max(2, round(src_info["height"] * pre)))
+                    extra.append(f"scale={pre_w}:{pre_h}:flags=lanczos")
+                    self.say(f"the AI runs at its native {scale}x on a "
+                             f"{pre_w}x{pre_h} frame")
```

**Not verified on a card.** The reasoning comes from the engine's own code
and from the worker's `--resolution` call. The picture has not been
compared, and the per-piece time may rise (the guide is bigger), so this
wants a 20 s sample before a film. The estimate is at the bottom of this
file.

---

## 2. The finishing grade crushes the shadows and pulls the highlights down

**Where:** `engine.py`, the `eq=contrast=` stage of the finishing chain,
and the `LOOKS` table in the same file.

**The measurement.** The same 400 frames of the saved AI output, through
each stage of the *Enhance* look, at 320x240:

| stage | mean | p5 | p50 | p95 | p99 | black <=5 | white >=250 |
|---|---:|---:|---:|---:|---:|---:|---:|
| the AI output | 62.83 | 1.0 | 41.0 | 197.0 | 251.0 | 13.10% | 1.115% |
| + 18% lanczos (the soft branch) | 62.52 | 1.0 | 41.0 | 197.0 | 250.0 | 13.41% | 1.081% |
| + cas 0.2 | 62.43 | 1.0 | 41.0 | 197.0 | 250.0 | 13.60% | 1.091% |
| **+ eq contrast 1.02** | **60.60** | **0.0** | **38.0** | 197.0 | 252.0 | **19.00%** | 1.215% |
| + colorlevels lift 0.01 | 60.28 | 0.0 | 38.0 | 195.0 | 249.0 | 16.79% | 0.990% |

**The conclusion.** `eq=contrast=1.02` pivots around mid-grey. The picture
is dark - its median is 41 out of 255 - so everything below mid-grey goes
down: the median loses 3 levels and **5.4% of the pixels land on pure
black**. The `colorlevels` lift then buys the blacks back by giving the
highlights away (p99 252 -> 249).

For comparison, the source file itself: mean 67.3, p50 44, p95 199,
p99 251, 10.4% black, 1.12% white. The finished film should not be
*heavier* than the source it came from.

**The alternatives, measured.** Same 400 frames, after `cas=0.2`:

| grade | mean | p5 | p50 | p95 | p99 | black <=5 | white >=250 |
|---|---:|---:|---:|---:|---:|---:|---:|
| the AI output, ungraded | 62.83 | 1.0 | 41.0 | 197.0 | 251.0 | 13.10% | 1.115% |
| current: contrast 1.02 + lift .010 | 58.09 | 0.0 | 35.0 | 195.0 | 250.0 | 21.39% | 1.055% |
| contrast 1.02 + lift .030 | 55.54 | 0.0 | 31.0 | 194.0 | 250.0 | 26.13% | 1.076% |
| **gamma 1.08 + lift .012** | **65.70** | **2.0** | **44.0** | **200.0** | 251.0 | **11.82%** | 1.153% |
| gamma 1.12 + lift .012 | 68.81 | 4.0 | 48.0 | 203.0 | 252.0 | 7.71% | 1.227% |
| saturation only + lift .020 | 58.71 | 0.0 | 36.0 | 194.0 | 249.0 | 19.94% | 0.959% |
| gamma 1.08 + sat 1.05 + lift .015 | 64.97 | 1.0 | 43.0 | 200.0 | 251.0 | 13.28% | 1.183% |
| lift .02 + pull the white to .99 | 58.03 | 0.0 | 35.0 | 192.0 | 246.0 | 19.94% | 0.430% |

**Not decided.** The gamma grades hold the shadows and give the midtones
back; the contrast grades do not. But "gamma 1.08" and "gamma 1.12" are
different pictures, and which one is right is a judgement about the look.

Two more things the same measurement points at:

- `LOOKS["polished"]` uses contrast 0.03 and `LOOKS["matte"]` 0.02 - the
  same knob, pushed harder. They crush harder.
- The `enhance` look is meant to be "denoised, crisp, with depth". A
  contrast bump is not depth; the depth in the Topaz reference comes from
  the two passes at **-30** (smoothing), which the research in
  `docs/review/dvd-look-research.md` describes as the thing that carries
  the look, with the AI there only as a fidelity step.

---

## 3. The card list offers a card SeedVR2 cannot use

**Where:** `rental.py`, the `GPU_CHOICES` table and `vram_for()`.

SeedVR2's weights are FP8 (`seedvr2_ema_3b_fp8_e4m3fn.safetensors`, see
`worker/diffusion_job.py`). An A100 is Ampere: it has no FP8 tensor
cores, so the 80 GB of VRAM buys nothing and the card is slower than an
L40S for this model - or unusable, depending on the kernel. The card list
still offers `NVIDIA A100 80GB PCIe` for SeedVR2 because the filter is on
VRAM alone.

**Not decided.** Either filter the card list on the engine's arithmetic
precision, or leave the A100 available and let the measurement decide.

---

## 4. The GUI has no resume

**Where:** `cloud.py`, `upscale_chunked()`.

A job that dies - a dropped connection, a full disk, a mistake - redoes
every piece from the first. The finished answers are sitting in
`work/jobs/<id>/parts/answer-*.mp4`, and `tools/finish_film.py` (written
for exactly this) skips them. The GUI path does not.

**Not decided.** Whether the resume belongs in `upscale_chunked()` (skip a
piece whose answer already exists and is the right size) or in the server.

---

## The estimate for checking any of this on a card

Nothing above has been run on a card, because renting one needs an
approval first. When it is wanted:

| item | number |
|---|---|
| card | NVIDIA L40S, $1.09/h (secure) or $0.79/h (community) |
| spin-up | 4 min (the image carries the SeedVR2 weights, so there is no weight fetch) |
| a 20 s sample, 10 pieces | 10 x 66 s = 11 min of card time |
| **total for one 20 s comparison** | **$0.29 to $0.40** |
| the last 22 pieces of the film (1081 frames) | 23 min of card time, $0.53 measured |

The measured rate is 66 s per 50-frame piece at 1440x1080 on an L40S.
