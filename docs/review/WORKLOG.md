# The worklog

Everything in this repository was built in one session, on a Mac, against a
real problem: DVD and SD recordings of Serbian television that need to
become sharp 1080p films.

This is the map. `FINDINGS.md` has the open questions with their
measurements. `CLAUDE-PROMPT.md` is the brief for a reviewer taking this
over.

---

## The short version

- **`upscaler-gui`** is a local GUI (`index.html` + `server.py` on port
  8790) that upscales a video with one of four engines and finishes it with
  a grade. It runs on the Mac's own GPU for free, or in the cloud on a
  rented Runpod card for a few tens of cents.
- **`drm-capture/`** is a separate tool: it captures DRM-protected
  streams from a browser through the Chrome DevTools Protocol, which is how
  the MNE Play material in the review images was obtained.
- The **last piece of work** was a full 211-second film - *Zvezde Granda -
  Milica Pavlovic* - upscaled end to end with SeedVR2 on a rented L40S,
  finished with the *Enhance* look, and muxed back with its original audio.
  It cost **$0.53** and it is in the review media.
- **39 guards** hold (`tools/test_cloud_guards.py`). They are the
  regression net for everything below.

---

## The two paths

**Local.** The Mac's GPU runs Real-ESRGAN through the `upscalayl` binary
in `worker/vendor/`. It is free and it never leaves the machine. A
separate worker process serves it on port 8791.

**Cloud.** A pod on Runpod runs the model in a container. The pod is
created, the video is uploaded in pieces, the answers come back piece by
piece, the pieces are joined, and the finishing runs on the Mac. The
engines that need it - SeedVR2, DLoRAL, STAR - are diffusion models and
are only worth it on a card.

Both paths share `engine.py`: the same staging (crop, de-interlace,
denoise), the same finishing chain, the same grade.

## The file map

| file | what it is |
|---|---|
| `index.html` | the GUI. One page, no build step. |
| `server.py` | the API and the job state machine. Port 8790. |
| `engine.py` | the pipeline: staging, the pre-scale decision, the finishing chain, the looks, the presets. The heart of it. |
| `cloud.py` | the rented path: the upload, the piece loop, the join, the disk guard. |
| `rental.py` | the Runpod side: the card catalogue, the prices, the VRAM rules, the pod lifecycle, the approval gate. |
| `star.py`, `rife.py`, `lifecast_denoise.py`, `ffbin.py` | the STAR clip, motion smoothing, the bilateral denoiser, and the resolver that finds an ffmpeg that actually runs. |
| `worker/` | what runs inside the container: the API, the diffusion job, the DLoRAL and STAR jobs, the entrypoint. |
| `tools/` | the guard suite, the volume tool, the OpenRouter ledger, the API-anything CLI, `finish_film.py`. |
| `api-anything/` | the agent-facing description of this API. |

## The engines, and what they cost

Everything below was **measured on a card**, not estimated from a paper.

| engine | what it is | measured | cost |
|---|---|---|---|
| Real-ESRGAN | a faithful enlargement | local, free | - |
| **SeedVR2** | a 3B diffusion transformer, one step, temporally consistent. **Restores** - it does not enlarge. | 66 s per 50 frames at 1440x1080 on an L40S | ~$0.53 for the 211 s film |
| DLoRAL | SD 2.1 as a video restorer, works from a quarter-size canvas | 10.45 s/frame at 1080p | 10.8 h / **$11.83** at 1080p; 3.5 h / **$3.15** at 720p |
| STAR | a steerable diffusion: a text prompt, a small clip, a regeneration | 320x180 to 1280x720 in 906 s | ~$0.25 |

The DLoRAL cost model is `0.114 + 3.753 * Mpx^1.427` seconds per frame -
refitted on a measurement, not guessed.

**The VRAM wall.** `vram_for(engine, out_height)` decides what a job
needs, and the card list follows it. SeedVR2 at 1080p needs **48 GB** - it
runs out of memory on a 4090 (`torch.OutOfMemoryError` in
`attn_video_vae.py`). A DLoRAL job capped at 720p drops from $5.65 to
**$2.14** because it fits on a 24 GB card.

**The cards.** L40S $1.09/h secure, $0.79/h community. A100 80GB PCIe
$1.79. H100 PCIe $2.89. RTX 4090 $0.89. The catalogue names are the real
ones - `NVIDIA RTX A5000`, not "A5000", and `NVIDIA A100 80GB PCIe`, not
"A100 80GB".

## The looks

A look is the finishing, and it is what the picture actually looks like at
the end. `LOOKS` in `engine.py`:

- **enhance** - denoise first, keep the diffusion's detail, CAS, grain,
  halation. The default preset.
- **clean** - the faithful enlargement and nothing else.
- **polished** - the "DVD look": soften twice, blend 35% of a plain
  enlargement back in, grain.
- **matte** - flatter and softer still.
- **clean8** - clean plus a denoise, no grain, no grade.

The soft looks blend the AI result with a plain lanczos enlargement
(65/35) and then soften, because an AI upscale on its own looks waxy. The
research behind that is in `docs/review/dvd-look-research.md`: in the
Topaz recipe the look comes from the finishing, not the AI - two passes at
**-30**, the newer model composited at **65%**, and noise at **3** to
conceal the patches that MPEG recovery leaves washed out.

**Face restoration is off.** GFPGAN, RestoreFormer and CodeFormer are in the
image as a layer and all dormant (`UPSCALER_ALLOW_FACE_PASS`). RestoreFormer
over an upscaled video reads as a retouched face rather than as film. It is
not offered and it is not run.

## The money rules

- The estimate is computed before anything is created, and it speaks in
  hours and dollars.
- The approval is **server-issued**: `/api/estimate` returns an approval
  id, the user approves that id, and the job carries it. An agent can ask
  for a price; it cannot approve its own spending. An unapproved agent run
  was refused with `"write":"not_applied"`.
- The cards are created, used and destroyed. `keep warm` stops the pod
  instead of terminating it, so the next job starts instantly.
- Pod ids are written to `~/.upscaler-gui-pods.json` and swept at server
  start: a mid-job restart once orphaned a pod that billed $0.38.

## API Anything

`api-anything/upscaler-gui.json` describes this tool to an agent in seven
operations - `presets`, `estimate`, `run_local`, `run_cloud`, `job`,
`cards`, `approvals` - and `api-anything/openapi.json` describes the
same API in eleven paths. `tools/api_anything.py` is the CLI. The agents
(Claude Code and the desktop app) are connected; sharing is off.

The point of the `approvals` operation is the gate above: an agent can
drive the whole tool and still cannot spend the user's money without the
user saying yes.

---

## What broke, and how it was found

These are the failures that shaped the code. Each one is a trap someone
else would fall into again.

**The entrypoint's weight links.** The DLoRAL symlink could not be made -
the parent directory did not exist, and an empty STAR folder had to be
replaced by a symlink rather than filled. The weights are not baked into the
image on purpose (they are 11.8 GB); SeedVR2's are, because they are 3.4 GB
and the image is meant to be usable on its own.

**The "clean" look died on an unconnected `[soft]` chain.** A finishing
pass that throws away the upscale that already worked is worse than no
finishing, so the post pass is now wrapped so that a failure is a message,
not a lost result.

**DLoRAL lost a frame at every seam.** The pieces overlapped by nothing, so
a film of 106 pieces came out one frame short at every join. One frame of
overlap per piece fixed it; the guard now checks that 10 frames in give 10
frames out.

**The finishing crushed the shadows and ate the highlights.** This is the
one that is still open - see `FINDINGS.md`. The measurement is there: the
`eq=contrast` stage takes 3 levels off a median of 41 and pushes 5.4% of
the pixels onto pure black.

**The cost model was a guess.** It was refitted on a measured 10.45 s per
frame, which moved 1080p DLoRAL from a made-up number to 10.8 hours.

**The 48 GB wall.** A 1080p SeedVR2 job was being offered a 4090. The
card list now follows the size of the result.

**The Docker store was broken.** The base image had to be pulled **by
digest** - the legacy builder looks a multi-arch base up by the *host*
platform's manifest, and the arm64 one has no torch 2.5.1. The build then
completed all 58 steps.

**The network volume cost more than it saved.** $2.80 a month against about
$0.05 a run: it only pays after roughly 30 runs a month. It is **off by
default** now (`DEFAULT_VOLUME_ID = ""`) and the volume was deleted;
`tools/volume.py` turns it back on. With no volume the card can come from
any data centre, which is the second reason it was turned off: a pod can
only mount a volume from its own data centre, and that pinned the job to
one pool.

**A 71 GB datadisk filled the disk and killed a rented job at 80%.** The
job was 84 of 106 pieces in when the host ran out of space and the download
died with `Errno 28`. The cause was the leftover Colima build area, not the
user's data. `cloud.py` now checks the free space before every piece
write and fails with a message that says what to do.

**A preview I built was contaminated with a screenshot of a web page.** The
"Enhance" preview had a Streamlabs onboarding page composited over it at
16% opacity. It was found by solving the blend algebraically from two
frames (`e = (1-a)*r + a*still`) to recover the composited still, and then
proving its absence from the source, the guide, the staged master and the
AI output with a high-pass template - all four sat on the same baseline
(0.112) and only the contaminated file scored above it (0.241). The preview
was rebuilt; the project's own finishing code was never involved.

**Two bugs in that rebuild.** The source branch was not trimmed, so ffmpeg
kept the last AI frame alive for the whole source and a 20 s preview came
out 211 s. And the post pass renders its grain pattern only if the file is
absent, so it reused a 211 s pattern left over from an earlier render and
padded the output to match. Both fixed: every render now gets its own work
directory.

**The guard suite hung.** ffmpeg's `open()` for writing blocked for eight
minutes on an iCloud file macOS had evicted when the disk filled. The test
now deletes the old clip first.

---

## Where it stands

Verified at the end of the work:

- The film: **5281 frames, 1440x1080, 25 fps, 211.24 s**, AAC stereo at
  44.1 kHz. The frame count is exactly the source's.
- The join between the 84 old pieces and the 22 new ones is invisible: the
  frame-to-frame difference at the join is **10.93**, below the film's
  median of 17.09, and it ranks 4520th of 5280.
- All 105 piece boundaries are statistically indistinguishable from ordinary
  motion (median 16.91 against 17.09 elsewhere).
- The overlay sweep on the finished film is at baseline (0.105 against
  0.112).
- The pod is terminated and `GET /v2/pods` returns none.
- 39 guards hold; both services answer.

## What is still open

1. **The SeedVR2 guide is a quarter of the target** - the single biggest
   quality lever left. Measured, reasoned, ready to apply, not applied.
   `FINDINGS.md` section 1.
2. **The finishing grade** crushes shadows and pulls the highlights down.
   Seven candidate grades are measured. `FINDINGS.md` section 2.
3. **The A100 is offered for SeedVR2**, whose FP8 weights it cannot use.
   `FINDINGS.md` section 3.
4. **The GUI has no resume** - a dead job redoes every piece.
   `FINDINGS.md` section 4.
5. The A100 card is also still in the list for engines that could use the
   48 GB cards' spare capacity more cheaply.

## The money, in total

The rented cards used across this work: the STAR measurement, the DLoRAL
refit, the DLoRAL 1080p and 720p measurements, the film that died at 80%,
and the 22 pieces that finished it. Roughly **$2.20** in total, the bulk of
it the two DLoRAL measurements. The finished film itself was $0.53.
