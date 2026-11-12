# Review notes: the first engagement

What a reviewer found, what changed, what was rejected, and what is still
open. `FINDINGS.md` has the four starting points; this is what became of
them, plus what nobody had mentioned.

**Money spent on cards: $0.00.** The sandbox's network policy refuses
`api.runpod.io` (403 at the proxy), so no card could be rented, and the
SeedVR2 A/B the brief asked for has not been run. `GET /api/spend` at the
end: `{"cap": 25.0, "total": 0.0, "remaining": 25.0}`.

**The guards:** 87 pass. 1 fails as expected (`the local worker answers`:
it needs the macOS `upscalayl` binary). 2 are skipped and say so: the
iCloud check (not a Mac) and the catalogue check (RunPod unreachable).

---

## The finishing grade (FINDINGS 2): changed

**What was measured.** 100 frames of the raw SeedVR2 output
(`film-preview-raw.mp4`), through the real `cloud.finish_locally`, one
stage at a time, on the Y plane in code values (the files are untagged
yuv420p, so they are limited range: 16 is black and 235 is white).

| | mean | p5 | p25 | p50 | p95 | p99 |
|---|---:|---:|---:|---:|---:|---:|
| the raw AI output | 57.4 | 18 | 24 | 41 | 158 | 213 |
| `eq=contrast=1.02` alone | 55.0 | 15 | 21 | 38 | 157 | 214 |
| `colorlevels=rimin=0.01` alone | 54.8 | 16 | 23 | 38 | 155 | 211 |
| **the old Enhance chain** (no grain) | **52.7** | 16 | 21 | **35** | 154 | 212 |
| **the new Enhance chain** (no grain) | **60.0** | 19 | 27 | **45** | 160 | 214 |

Two things FINDINGS did not have:

- `colorlevels=rimin` is the **input** black point. It was meant as a
  lift and it is a crush: alone it takes p5 from 18 to 16. It also only
  works in RGB, so ffmpeg took every frame to RGB and back, which clips
  the saturated stage lights (p95 158 -> 155).
- Together with `eq=contrast`, the chain took the median **six** levels
  down (41 -> 35), not three. The preview in the release (median 38) was
  made by an earlier build and is milder than what the code made.

**What it is now.** `engine.grade_filters`: one curve on the luma only,
`out = lift + (1 - lift) * x^(1/gamma)` between 16 and 235, white pinned,
saturation on the chroma only, no RGB round trip. Enhance, Polished and
Matte use gamma 1.04 with their existing lifts.

**Why 1.04 and not 1.08.** FINDINGS put the source's median 3 display
levels above the AI output. Gamma 1.04 adds about 4.7, gamma 1.08 about 8.
At 1:1 (`images/grade-fix-1to1.png`: the raw output, the old grade, 1.04,
1.08), the old grade turns the man's hair into a solid black shape and
sinks the woman's hair into the backdrop; 1.04 is the closest to the raw
output with the shadow separation back; 1.08 opens the hair most but
brightens the faces past the raw output and starts lifting the black
backdrop toward maroon. **This is the taste call left for you:** 1.08 is
defensible if you want the film brighter than the source.

**Not measured:** the perlin grain. It needs ffmpeg 7.1 and the sandbox
has 6.1 (see below). The grain is level-neutral by construction (its bias
is measured and subtracted), so it should not move these numbers, but that
has not been confirmed on this material.

## The SeedVR2 guide (FINDINGS 1): changed, NOT verified on a card

FINDINGS' numbers were not what the GUI does. The Enhance preset sends
`model: ""`, the server fills in the x2 model, so the guide was
target / **2**, not / 4. And with the GUI's default cap ("no limit")
there is no target, so no pre-scale at all. The bug is real where it
bites: an **HD source with a cap** (a 1080p source capped at 1080p went up
as 960x540).

FINDINGS proposed blowing the guide **up** to the target. Rejected: an
upscale adds no information, only upload bytes through a proxy measured at
about 90 KB/s with a 100 s limit, and the CLI resizes to `--resolution`
itself. The guide is now the source as it is, brought down only when it is
bigger than the target (`engine.guide_for_restore`).

**What was found on the way:** the GUI's SeedVR2 route (`/upscale` ->
`seedvr2_upscale`) ran the CLI without `--model_dir`, `--dit_model` or
`--color_correction`. The CLI then looks for weights in
`./models/SEEDVR2` under `/app/seedvr2`, not in
`/app/seedvr2_models/SEEDVR2` where the image bakes them. The worker now
passes the same three as the `/diffusion` route. **It needs the image
patched** (`worker/Dockerfile.patch`) to reach a card.

**Still to do, on a card:** the 20 s A/B, old guide against new, on an HD
source with a cap. The estimate: 658 s of L40S time each, about $0.27
each with the spin-up.

## The A100 (FINDINGS 3): changed, for a different reason

SeedVR2 had no card list of its own; it fell through to Real-ESRGAN's,
which is how the A100 was offered. **The FINDINGS reason is very likely
wrong:** the SeedVR2 code keeps FP8 weights as storage and computes in
BF16 (its 2025-08-07 release notes), so an Ampere card can run them. The
A100 is left out on price and evidence: never measured on this model, and
about 64% dearer an hour than the L40S it was measured on.

## The resume (FINDINGS 4): changed, and not where FINDINGS put it

A retry from the GUI is a **new job with a new folder**, so a resume inside
`upscale_chunked()` alone would never have found the old pieces. The
pieces now live in `work/jobs/_resume/<key>`, keyed on the source file and
every setting that shapes the answer. Keyed on the source, not the upload's
bytes: the upload is re-encoded each time and x264 does not give the same
bytes twice (checked: two runs, two hashes). The diffusion, DLoRAL and STAR
routes have no resume yet.

---

## What nobody had mentioned

Ordered by what they could cost.

1. **The approval gate could be walked through.** `/api/run` swallowed its
   own exceptions and the job then took `rent_approved` from the request,
   so `"rent_approval": [1]` (the lookup raises on a list) rented a card
   with no approval and no cap. Fixed: the gate fails closed and the
   client's approval fields are thrown away. Not exploited to prove it;
   the guard that checks it is built so that even a broken gate rents
   nothing.
2. **Any caller could approve its own price.** `/api/approve` had no
   check, and the agent API description listed it. An approval now needs
   a token that exists only inside the GUI page. (It stops the API being
   used to approve. It cannot stop someone with a shell on the Mac.)
3. **A web page could drive the server.** No Host or Origin check: a
   cross-site POST or a DNS-rebound name reached it. Both are refused now.
4. **Stop did not stop a rented job.** `cancel()` set a flag no cloud
   loop read; the card kept working, and billing, to the end.
5. **The cap capped guesses.** It booked `one_clip_cost`, which leaves out
   a cold card's weight fetch, and never looked at the real bill. It now
   books the cold price under a lock, stops the job itself past what the
   cap had left, and settles on the card's measured time.
6. **`terminate()` trusted the DELETE**, which the brief itself says can
   answer 200 and leave the pod billing. It now re-reads the pod and
   retries; a pod that will not go stays in the sweep file.
7. **De-interlace "auto" could never fire.** `video_info` never returned
   `field_order`, so an interlaced DVD capture went to the AI combed.
8. **The grained looks died on ffmpeg 6.1** (Ubuntu 24.04, Debian):
   `perlin` arrived in 7.1, the generator failed silently, and the
   finishing pass died at the very end, after the card was paid for. Now
   it falls back to the static grain. Your Mac's Homebrew ffmpeg is not
   affected.
9. **The estimate was 13% short for SeedVR2**: it left out the round trip
   of every 50-frame piece (8 s each, measured).
10. **A DRM content key is in the repository**
    (`drm-capture/presets/mne-play.key`: a KID and a CEK for the MNE Play
    stream). The release downloads without authentication, so the
    repository looks public. **Not changed** - purging it means rewriting
    the history of a branch that is not mine. Your call.
11. **The docs disagree about money.** "$0.53 for the film" is the last
    22 pieces (FINDINGS' own table); the whole film is ~106 pieces x 66 s,
    about $2.10 of L40S time plus spin-ups - which also does not fit the
    "$2.20 in total for every card in the work" in the worklog. The Enhance
    preset said a 72 s film is $1.60; it is about $0.80.
12. **Stale copies at the root**: `worker_server.py` and
    `worker_Dockerfile` differ from `worker/` by hundreds of lines and
    nothing uses them; `tools_collage.py` and `tools_e2e_rental_test.py`
    are exact duplicates. Not deleted.
13. `keys/` (where `keys.py` reads keys from) was not in `.gitignore`.

## What the brief got wrong

- "Expect exactly one failure": on Linux there were two; the iCloud check
  failed too. It now skips off a Mac.
- "The cap ... refuses any job that would take the session past it": it
  refused on the estimate, and nothing watched the job once it ran. It does
  now.
- FINDINGS 1's numbers (scale 4, 360x270) are not the GUI's path, and
  FINDINGS 3's reason (no FP8 on Ampere) does not hold. Both conclusions
  survive for other reasons.

## Next

1. ~~**Allow the sandbox to reach RunPod**, then run the SeedVR2 A/B.~~
   **Done on the user's Mac instead** - the sandbox could not reach
   RunPod, the Mac can. The result is in `SEEDVR2-AB.md`: the guide
   resolution moves the detail by **+0.8%** and the sharpness by
   **-0.9%**, and the brightness by **+5.1 levels**. The guide is not the
   lever. The finishing is.
2. ~~**Patch and push the SeedVR2 image with the worker change.**~~
   **Done.** `bzz1/upscaler:seedvr2` is now
   `sha256:30e962120691064a45bcecea71aec48be413098a88ba999ae727e97104d13915`
   (the old one, `sha256:4487ac3b0f2d`, is still pullable by digest). The
   image now hands the SeedVR2 CLI the weights it carries, so a pod stops
   fetching them from Hugging Face and uses the measured 3B FP8 model.
   `worker/entrypoint.sh` is also marked executable in git, which is why
   the patch can be one layer.
3. **Resume for the diffusion, DLoRAL and STAR routes.** Still open.
4. **Decide gamma 1.04 against 1.08 by eye**, on a full clip with the
   perlin grain (needs ffmpeg 7.1). Still open - this is the taste call,
   and the images are in `images/grade-fix-1to1.png`.
