# Video Upscaler — a small GUI for Holloway's Upscaler

A local web GUI that drives [Holloway's Upscaler](https://github.com/hollowaykeanho/Upscaler)
(Real-ESRGAN NCNN) on this Mac, and makes video upscaling actually usable
here.

```bash
cd ~/upscaler-gui
./start.sh          # the GUI and the local worker
open http://127.0.0.1:8790/
```

`./start.sh stop` shuts both down and `./start.sh status` says what is
running. To run the GUI by hand instead:

```bash
cd ~/upscaler-gui
python3 server.py --port 8790
```

Binds to `127.0.0.1` only. Nothing leaves the machine.

If `vendor/` is empty (it is not included in the source archive, because it is
222 MB), fetch the upstream release with:

```bash
./install.sh
```

---

## What is in the box

```
upscaler-gui/
├── server.py         the local server + the API
├── engine.py         the upscale pipeline
├── index.html        the UI
└── vendor/upscaler/  the upstream release, untouched
    ├── bin/mac-amd64     the Real-ESRGAN binary
    ├── models/           the AI models
    └── start.cmd         the upstream script (not used by the GUI)
```

The upstream release is bundled as it was downloaded from
<https://github.com/hollowaykeanho/Upscaler/releases/tag/0.7.0>.

---

## Does it even run on this Mac?

Yes, and on the GPU.

The upstream README lists `darwin-amd64` as **FAILED** and the release only
ships `bin/mac-amd64`. That file turns out to be a Mach-O **universal**
binary with a native `arm64` slice, and macOS 26 provides Vulkan through
MoltenVK, so the binary finds the GPU:

```
[0 Apple M1 Pro]  queueC=0[1]  queueG=0[1]  queueT=0[1]
[0 Apple M1 Pro]  fp16-p/s/a=1/1/1  int8-p/s/a=1/1/1
```

No Docker, no Linux VM, no Rosetta needed.

---

## What the GUI does differently from the upstream script

The upstream `start.cmd` extracts every frame, runs the AI binary **once per
frame** and then reassembles the video with ffmpeg. Three things were changed:

1. **One process per chunk instead of one per frame.** The binary accepts a
   directory and processes it in a single process, so the model is loaded
   once per chunk rather than once per frame. On this machine that is the
   difference between **1.3 s** and **44 s** per frame.

2. **The frames are piped, not stored.** The upscaled PNGs go straight into
   one long-lived ffmpeg through a pipe. The upstream script writes every
   frame to disk first, which for a 4K upscale is tens of gigabytes.

3. **The source is normalised to constant frame rate / bit rate first.**
   The upstream tool only handles CBR video, and a VBR source produces a
   broken result.

---

## Using it

1. Pick a video from the list (it watches `~/Videos`, `~/Movies`,
   `~/Downloads`, `~/Desktop`, `~/drm-capture/out`) or drop one on the page.
2. Pick a model and a scale.
3. Press **Upscale**.

The result appears in the page with a download link.

### Output settings

| Output | Notes |
|---|---|
| **H.264** | mp4, plays everywhere. CRF 17, visually lossless. |
| **HEVC / H.265** | mp4, roughly half the size for the same quality. `hvc1` tag so QuickTime and Apple TV like it. |
| **HEVC 10-bit** | p010le. Keeps the gradients in a broadcast picture smooth instead of banding. Twice the size. |
| **ProRes 422** | mov, for editing. Enormous files. |
| **VP9** | webm, matches what the recorder produces. |

---

## Lifecast's UpscaleVideo.ai — and its recipe, ported

Lifecast.ai shut down and **open-sourced its whole toolchain**
([fbriggs/lifecast_public](https://github.com/fbriggs/lifecast_public), MIT):
4D Gaussian Studio, UpscaleVideo.ai, the Volumetric Video Editor, Volurama,
the NeRF engine, and the WebGL / WebXR / Unreal / Unity volumetric players.

### UpscaleVideo.ai is a finished Mac app

* `Lifecast_UpscaleVideo1.17.dmg` (165 MB) in the
  [upscale_video-1.17 release](https://github.com/fbriggs/lifecast_public/releases/tag/upscale_video-1.17)
* The binary inside is **`Mach-O 64-bit executable arm64`** — native Apple
  Silicon, so it runs on an M1 Pro with no Rosetta.
* The models are bundled with it (`edsr2_noaug.pt`, `edsr2_mystery.pt`,
  `unet_noaug.pt`, `unet_mystery.pt`, plus `rof_cpu.pt` for the optical
  flow).

It does: 2x up to 16K, de-noise, de-blur/sharpen, content-adaptive
restoration, h264 / h265 / 10-bit ProRes / 16-bit PNG, drag and drop, batch.

To install: download the .dmg, open it, drag the app to Applications. macOS
will refuse the first launch ("cannot be opened because the developer cannot
be verified") — right-click it and choose **Open**, then confirm.

### Their recipe, ported into this GUI

From `lifecast_apps/source/upscale_video.cc`:

```cpp
const float sigma = bilateral_filter_radius * 0.6;
const float sigma_color = 0.1;
cv::Mat bilateral = opencv::bilateralDenoise(src, radius, sigma, sigma_color);
prefiltered = bilateral * strength + src * (1.0 - strength);
```

with their defaults `radius = 7`, `strength = 0.85`. Then a 2x AI pass with
UNet (faster) or EDSR2 (higher quality), then the encode.

Two details that matter and that are now in this GUI:

* the de-noised frame is **mixed back with the original** by the strength
  factor rather than used on its own;
* it runs on the float image, so the mixing does not band.

**De-noise → Bilateral, as in Lifecast's UpscaleVideo.ai** does exactly this,
in OpenCV, with their defaults. ffmpeg has no bilateral filter of its own
(the ffmprovisr build adds one), so this is done with OpenCV on the
extracted frames — about 53 ms per 1080p frame, which is nothing next to the
AI pass.

Their **models cannot be reused here**: the `.pt` files are custom C++
classes (`enhance::Base_SuperResModel`) and the architecture lives in their
closed-source 4dgstudio library, so the weights are meaningless without it.
This GUI uses Real-ESRGAN in that slot instead.

---

## What was worth taking from elsewhere

Four projects that do the same job, and what each of them contributed.

**QualityScaler** (Windows, ONNX/DirectML, CustomTkinter) — its best idea is
"**interpolation between the original file and the upscaled file**", with
Low / Medium / High levels. That is exactly what the finishing pass does
here, and it is the standard way to take the invented detail back out of an
AI upscale. It is now a **Softness** slider you can move by hand:

> 0 = pure AI (sharpest, most invented detail) · 0.35 = a film-like result
> · 0.7 = mostly the plain enlargement

Its other ideas — tiling to stay inside the VRAM, and resizing *before* the
AI — we already do (the de-noise happens before the AI for the same reason:
an upscale amplifies noise into detail).

**EnhancR** (Electron, TensorRT + NCNN, Windows/macOS) — the same
interpolation idea, plus two things worth knowing about:
* its NCNN engine runs on **Apple Silicon**, which is the engine this tool
  uses too;
* its benchmark is the number that justifies renting: **RealESRGAN
  animevideov3 at 38.67 fps** on an RTX 4090 (1080p to 2x). This Mac does
  **0.58 fps** on the same model — about 65x slower. A rented 4090 at
  $0.44/hour therefore turns a 60-second clip into about 80 seconds of
  card time, which is **pennies**.

Its "encode on the fly, no frames on disk" trick is not available here:
the NCNN binary takes a file or a directory, and `-i -` / `-o -` fail with
"invalid outputpath extension type". The frames have to land on disk, which
is why the chunks are kept small (30 frames).

**davlee1972/upscale_video** (Python + ncnn_vulkan) — confirms the shape of
the pipeline: extract frames in batches to a temp directory, upscale, then
merge. He uses ten-minute batches; this tool uses 30 frames, so the peak
disk use is far lower. Two things taken from it: **10-bit output**
(`p010le`, which keeps the gradients in a broadcast picture smooth instead
of banding), and the idea of running **several NCNN workers on one GPU** —
worth doing on a rented card, where the bottleneck moves from the GPU to
the CPU, and not on this Mac, where the GPU is already the bottleneck.

**Not worth taking:**
* **huggingface/transformers** — DLoRAL needs it anyway, and it is
  installed. Note that DLoRAL pins `transformers==4.28.1`, so the DLoRAL
  container keeps its own environment; do not mix it with this one.
* **InvokeAI** — a polished diffusion GUI, but for images. The diffusion
  route here is video-specific and already goes through DLoRAL.
* **dandere2x** and **R-ESRGAN-AnimeVideo-UI** — built for anime
  (DaNDanGare2x, Real-CUGAN, Anime4K). Good tools, wrong subject: they
  sharpen line art and remove colour bleeding. Broadcast TV is the opposite
  problem.

---

## Renting the GPU from the GUI

The **cloud** and **regenerate** presets hire a card when the job starts and
give it back when the job ends. You do not set up a pod, and you do not pay
for one sitting idle.

The lifecycle, all of it from the GUI:

```
  rent  ->  wait until the worker answers  ->  run the job  ->  destroy
```

The destroy is in a `finally`, so **the card is given back even if the job
fails or you press Stop**. That is the whole reason it is a module rather
than three lines in the job: an orphaned pod keeps billing you by the hour.

### The weights, and why a run used to spend twenty minutes downloading them

The diffusion engines need their weights on the card: 11 GB for DLoRAL,
3.4 GB for SeedVR2, 7.6 GB for STAR. Hugging Face serves them at 5–9 MB/s,
so a run that fetched them spent **twenty minutes and $0.38** downloading
before it started working — and did it again on every run, because a
terminated pod takes its disk with it.

There are three ways to stop that, and the tool has all three:

| | what it does | what it costs |
|---|---|---|
| **Keep the card between jobs** | the card is *stopped* at the end of a job instead of terminated: the GPU is released, and the disk — with the weights on it — is kept. The next run of the same engine wakes it up and starts working at once. | free: a stopped pod with no volume disk of its own costs nothing |
| **The network volume** | the weights live on a volume that belongs to no pod: every pod that mounts it finds them already there. Survives termination, shared between pods. **Off by default** — see below. | $0.07/GB/month — $2.80 for the forty gigabytes the three engines need |
| **Baking them into the image** | they come with the image, from Runpod's own CDN. | a bigger image: a bigger pull on every run, and a big push once |

The **network volume** is **off by default** now, and that is a
money decision rather than a technical one:

| | |
|---|---|
| the volume | $2.80 a month, every month, used or not |
| what it saves | one weight fetch per run |
| the fetch | 11.8 GB at 72 MB/s = 2 min 43 s = **$0.05** |
| the break-even | about **30 runs a month** |

The fetch is not a fixed number: the same eleven gigabytes were measured
at 5–9 MB/s on another run, which would make it $0.79 instead of $0.05.
So the volume buys certainty, and it is worth it if you run the same
engine thirty times a month or more. Below that it is $33.60 a year for
something a card that is kept warm between jobs gives away.

Turning it on is one command and one line of code:

```
python3 tools/volume.py create --dc US-TX-3 --size 40
# and put the id it prints into RUNPOD_VOLUME_ID
```

Two things about it are worth knowing:

* **the card has to be in the volume's data centre.** A pod can only
  mount a volume from its own centre, so the volume narrows the choice of
  card to that centre — and the centre has to have the card in the
  *secure* cloud, because that is the only kind a network volume can be
  mounted from. If there is no card of the kind you want there, the job
  falls back to a card without the volume and the weights are fetched
  onto the card's own disk instead: slower, but it works, and the job
  does not fail.
* **the volume does not move with the job.** A pod's own volume disk is
  billed at $0.20 a GB a month while the pod is parked, which is why the
  code mounts the *network* volume instead: a parked pod then costs
  nothing beyond the volume itself.

The volume's data centre is the thing to keep an eye on, because it is
the thing that can quietly waste the money. It was created in US-NC-1,
which had no GPU of any kind — the volume could never have been used, and
it cost $2.80 a month for nothing. It now lives in **US-TX-3**
(`gb5ouapvqq`), which is one of the twenty-four centres that take a
network volume *and* had an L40S in it.

`tools/volume.py` looks after it:

```
python3 tools/volume.py list     # the volumes on the account
python3 tools/volume.py probe    # which centres have cards - costs nothing
python3 tools/volume.py create --dc US-TX-3 --size 40
python3 tools/volume.py delete --id gb5ouapvqq
```

`probe` is free because a pod that cannot be created is not billed, and
Runpod answers "no instances available" *before* it looks at the balance:
the difference between that answer and "your balance is too low" is
exactly which centres have cards in them.

**The estimate says what the first run really costs.** A diffusion engine
needs its weights, and a card that does not have them fetches them from
Hugging Face — eleven gigabytes for DLoRAL, seven and a half for STAR.
That time is billed like any other, so the estimate quotes it as a line of
its own: the clip, the cold start, and the total for the first run. An
estimate that leaves the fetch out quotes $0.06 for a job that costs
$0.14, and the difference arrives as a surprise on the bill.

How long the fetch takes is not a constant: a card measured 72 MB/s
(11.8 GB in 2 min 43 s) on one run and 5–9 MB/s on another, probably
because the CDN had the file cached near that card or did not. The
estimate uses 40 MB/s, which is the fast one discounted by half.

The SeedVR2 image carries its own weights, so it has no such line: they
come down with the image, from Runpod's CDN.

**Keep the card between jobs** is a checkbox in the GUI, next to the API
key. It is worth it when you are running several jobs in a row: the second
one skips the image pull, and with the weights on the card it skips the
download too. It can also backfire — the machine a parked card sits on
may have given its GPU to somebody else in the meantime, and then the card
cannot be woken up. The job says so and rents a new one.

When you are done, the **Throw away the parked cards** button clears them.

The **baking** is per engine, at build time:

```bash
docker build --build-arg BUILD_SEEDVR2=1 --build-arg BUILD_SEEDVR2_WEIGHTS=1 \
  -t <you>/drm-upscaler:seedvr2 .
```

### What you need

1. A Runpod account and an API key
   (<https://console.runpod.io/user/settings/api>).
2. Our container pushed somewhere Runpod can pull it:

```bash
cd ~/upscaler-gui/worker
docker build -t <you>/drm-upscaler:latest .
docker push <you>/drm-upscaler:latest
```

(For the diffusion engine, build it with
`--build-arg BUILD_DLORAL=1` and push that image too.)

#### Rebuilding the worker without rebuilding the world

The full build of the worker image takes about forty minutes: it
installs torch, the face stack and three separate venvs. When only the
worker's own code changed, none of that has to happen again:

```bash
cd ~/upscaler-gui/worker
docker build -f Dockerfile.patch -t <you>/drm-upscaler:both .
```

`Dockerfile.patch` lays the changed files over the image that is
already built. It takes **forty-five seconds**, and it is one layer
rather than five — the legacy builder copies the whole filesystem for
every single step, so five COPYs is five copies of a thirty-two
gigabyte image.

It is also the way out of a broken store. The base image
(`nvidia/cuda:12.1.1-runtime-ubuntu22.04`) is a multi-architecture
image, and the builder looks the platform manifest up by digest. If the
store has the tag but not the manifest — which is what happens when the
content store was pruned under disk pressure — the build stops at

```
Step 1/58 : FROM nvidia/cuda:12.1.1-runtime-ubuntu22.04
NotFound: content digest sha256:83079b...: not found
```

and a plain `docker pull` does not fix it, because the pull fetches the
index and the builder wants the manifest. The repair is to pull the
manifest by its digest:

```bash
# the amd64 one - this Mac is an arm64, and the worker runs on the card
docker pull --platform linux/amd64 \
  nvidia/cuda@sha256:2541299cf78eee8ee2b415782ad6f083bc4d351c4e40f104c776159bd483650e
docker tag nvidia/cuda@sha256:2541299cf78eee8ee2b415782ad6f083bc4d351c4e40f104c776159bd483650e \
  nvidia/cuda:12.1.1-runtime-ubuntu22.04
```

The wrong manifest is the other way of breaking it: the arm64 one has
no torch beyond 2.0.1 on the cu121 index, so the build fails at
`pip install torch==2.5.1`. The tag has to point at the **amd64**
manifest, because that is the image that runs on the card.

Then in the GUI, under **Advanced → Cloud GPU**: tick **Rent a GPU for this
job**, paste the key, and put your image name in **Container image**. The
GPU list is filled in for the engine you picked, with the hourly price.

**Check the key** tells you whether the API accepts it before you start a
job.

### Letting an agent drive it

The GUI is a local program with a JSON API, and
[API Anything](https://github.com/goodnight000/api-anything) is a way to
give that API to agents: it turns a website into operations an agent can
call, over plain HTTP, and it runs as an MCP server that Claude Code,
Codex, Cursor, Gemini CLI and VS Code all understand.

One command connects them:

```bash
python3 tools/api_anything.py install
```

It installs the operations into API Anything's own store
(`~/.api-anything/sites/upscaler-gui.json`) and connects the agents it
finds. Sharing stays **off**: these operations point at 127.0.0.1, so
there is nothing to share with anybody, and nothing of yours leaves the
machine.

What the agents get:

| operation | what it does |
|---|---|
| `presets` | the engines, the looks, the backends and the cards with their live prices |
| `estimate` | what a cloud upscale would cost, before anything is rented |
| `run_local` | upscale a video on this Mac's own GPU - free, no approval |
| `run_cloud` | upscale a video on a rented GPU - needs the approval |
| `job` | how a job is going: its state, its log, its output |
| `cards` | the cards an engine can run on, with their prices and memory |
| `approvals` | the cloud jobs waiting for the user to agree to a price |

An agent finds them the way it finds anything else:

```bash
npx -y api-anything search "upscale video"
npx -y api-anything call upscaler-gui presets
python3 tools/api_anything.py call estimate path=~/Movies/film.mov engine=dloral
```

**The money still belongs to the user.** An agent can ask what a job
costs, and it gets an *approval id* with the answer - but the id is
pending until the user says yes. The GUI shows the pending approvals
under **Advanced → Cloud GPU**, with the price and an Approve button, and
only then can the job run. An agent cannot approve its own spending: the
server refuses a rental that carries no approved id, and the approval is
bound to the clip, the engine and the card it was priced for.

### A spending cap, for when the agent is allowed to spend

The gate above is the default, and it stays the default: a job that rents
needs the user's approval of the price, every time.

Sometimes that is the wrong shape - an unattended run, or a session where
the user has already said "go" and does not want to be interrupted. Then the
server can be started with a ceiling instead:

```bash
UPSCALER_SPEND_CAP_USD=25 python3 server.py --port 8790
```

With a cap set, the server approves rentals itself, up to that ceiling, and
refuses the job that would take the session past it. The spend is written to
a ledger *before* the job starts, so it survives a restart of the server:

```bash
curl http://127.0.0.1:8790/api/spend
```

    {"cap": 25.0, "total": 0.53, "remaining": 24.47, "jobs": [...]}

The cap is the control, so it has to be a number the user chose, and the
ceiling is not the agent's to raise. With no cap set, nothing about the gate
changes: an agent still cannot approve its own spending.

The API describes itself as well, in a machine-readable document:

```
http://127.0.0.1:8790/api/openapi.json
```

### Thinking ahead: two dashboards in the loop

**[CodeMouse](https://codemouse.ai)** reviews every pull request with
Claude and GPT at once, and skips what a human or another bot has already
said. That is a second opinion on the diff *before* it reaches you: point
it at the repository and every change that goes up comes back with two
models' findings, which get fixed before you read them.

The project is a git repository now, with the scratch space and the
vendored binaries left out, so a pull request is a reviewable thing.

**[OpenRouter's observability](https://openrouter.ai/workspaces/default/observability)**
is the same feed `tools/insights.py` reads: every request, its model, its
tokens, its cost and how long it took. As a dashboard it is a bill. Read
as a loop it is feedback - which of the habits are expensive, which model
is worth its price, and whether the last change was worth what it cost.

```bash
python3 tools/insights.py --days 7
```

The key goes in the Keychain, never in a chat:

```bash
security add-generic-password -a "$USER" -s openrouter -w
```

**The same loop belongs to this program.** The rentals are the bigger
bill: an estimate is a promise, and a promise that is never checked is a
guess. Every job should record what the card really cost against what
the estimate said, and the differences should feed back into the cost
model - which is exactly how the DLoRAL rate (measured at 14.5 s a frame
against a model that said 5.1) and the SeedVR2 memory rule (24 GB that
turned out to be 48) were found.

### What it costs, and for how long

**The diffusion engines cost what the result costs.** DLoRAL was measured
at 10.45 s a frame for a 1080p result and 1.14 s for a 480p one (on an
L40S, 2026-11-02): the diffusion runs on the canvas - the target over
four - and the attention over it grows faster than the pixels do. So the
72-second film at 1080p is **10.8 hours and $11.83** on one card, the
same film at 720p is **3.5 hours and $3.15**, and SeedVR2 at 1080p - a
different model, a different look - is **1.7 hours and $1.56**.

The estimate quotes the number before the money moves, and says "over
four hours" when it is.

| GPU | $/hour | Enough for |
|---|---|---|
| RTX 4090 / A5000, 24 GB | ~$0.44 | Real-ESRGAN, SeedVR2, and **DLoRAL up to 720p** |
| L40S, 48 GB | ~$0.99 | Real-ESRGAN, DLoRAL, STAR |
| A100, 80 GB | ~$1.19 | DLoRAL, comfortably |
| H100, 80 GB | ~$2.90 | DLoRAL, fast |

**The card has to be able to hold the picture.** DLoRAL's VAE decoder
runs its attention at the size of the *result*, not the size of the
source, so the memory it wants follows the picture it is making: at 1080p
the attention alone is 16 GB and the whole pass does not fit on a 24 GB
card at all. A 720p result is a quarter of the pixels and does fit.

So the card list follows the size of the result: a DLoRAL job capped at
**1280x720** is offered the 24 GB cards, and it costs **$2.62** instead
of $5.65 for the 1080p one — it is the same diffusion at a quarter of
the pixels. Uncapped, it needs an L40S or bigger. STAR needs 48 GB
whatever the size: it is a 1.7B transformer and wanted about 39 GB for
its own small example.

The estimate says how much memory the card needs, so the number is on
the screen before the money is spent.

The pod runs for exactly as long as the job: a 7-minute clip is about 7
minutes of card, a few cents. The log says when the pod is up and when it
is terminated, and if the termination ever fails it prints the exact
`curl` to do it by hand — because a pod that is not terminated is a pod
that keeps costing money.

### Without any of that

The provider list also has **This Mac**, which starts the worker as a local
process and goes through the identical create/wait/destroy path. That is
how the flow was tested, and it is the fallback if you would rather not
rent for a small job.

---

## How all of it fits together

There are four upscalers in play now, and they are not rivals — they sit at
different points on a scale:

| | What it does | Needs | Where it runs |
|---|---|---|---|
| **Real-ESRGAN** | enlarges what is actually there | a GPU, any kind | here, or rented |
| **Lifecast's bilateral** | de-noises, edge-aware, mixed back | CPU | here |
| **DLoRAL** | one step of diffusion, temporally consistent, invents detail | CUDA, 24 GB+ | rented |
| **STAR** | many steps of diffusion, guided by a prompt | CUDA, 24 GB+ | rented, and out of quota |

The chain this GUI builds:

```
  capture (drm-capture)  ──▶  de-noise  ──▶  the AI pass  ──▶  finish  ──▶  the file
   in the browser            bilateral,      Real-ESRGAN or      blend with a
   decrypt + MSE + record    on the Mac      DLoRAL, rented      plain enlargement,
                                                or here          soften, grain, grade,
                                                                  encode - all on the Mac
```

**DLoRAL is the interesting one, and it is also the one that
shoots itself in the foot.** It is diffusion-based like STAR, but it
does it in *one* step instead of many, and it is built for temporal
consistency rather than for text-to-video. So it sits between the two: it
invents detail like STAR does, but it is far cheaper and it does not shimmer
frame to frame. It is also the closest thing to the "polished" look you
asked for, because a diffusion model trained on real degraded video
reproduces that soft, film-like texture rather than the hard, waxy look an
SR model gives you.

**The finishing pass is what makes the difference.** Whatever the AI did, the
last step on your Mac blends it with a plain lanczos enlargement, softens it
and adds static grain. That is what takes the "invented detail" edge off
everything, whether it came from Real-ESRGAN, DLoRAL or STAR.

And if you want the detail to be *yours*, DLoRAL's training code is in the
same repo: train the LoRA on your own footage and the model learns your
look.

---

## DLoRAL

[github.com/yjsunnn/DLoRAL](https://github.com/yjsunnn/DLoRAL) — NeurIPS
2025, "One-Step Diffusion for Detail-Rich and Temporally Consistent Video
Super-Resolution" (HK PolyU + OPPO Research).

It freezes a Stable Diffusion 2.1 UNet and merges two LoRAs into it — one
for temporal coherence, one for spatial detail — so the enhancement is a
**single** diffusion step.

* It is **CUDA only** (the script calls `.cuda()` on the uncertainty map),
  so it has to run on a rented card. It cannot run on an Apple GPU.
* Its stack is the older OpenMMLab one: diffusers 0.25, torch 2.0.1,
  xformers 0.0.20, `mim install mmcv-full`, `mmedit`.
* The weights are on Google Drive, not in the repository.

### The easy way to try it

There is a **Colab demo** — no GPU, no setup, just upload and enhance:

<https://colab.research.google.com/drive/1QAEn4uFe4GNqlJbogxxhdGFhzMr3rfGm?usp=sharing>

That is the fastest way to see whether you like it.

### The way that fits this tool

The worker speaks one protocol and can run either engine:

```
POST /upscale
  X-Engine: realesrgan | dloral
```

Build the image with DLoRAL in it:

```bash
docker build --build-arg BUILD_DLORAL=1 -t drm-upscaler-dloral .
```

and put the weights in `/app/DLoRAL/preset/models/`
(`ram_swin_large_14m.pth`, `DAPE.pth`, `checkpoints/model.pkl`).

### The images that are on Docker Hub

| image | what it can do |
|---|---|
| `bzz1/upscaler:latest` | Real-ESRGAN and the face pass |
| `bzz1/upscaler:seedvr2` | the same, plus **SeedVR2** (the 3B diffusion) |
| `bzz1/upscaler:both` | the same, plus **SeedVR2 and DLoRAL** |

The diffusion engines are opt-in layers, so each one costs an image of its
own. The weights are **not** baked in - they are fetched on the card the
first time they are needed (3.4 GB for SeedVR2, 11 GB for DLoRAL), which
keeps the images small enough to pull quickly.

Point the **GPU worker image** field at one of these and the presets that
need an engine the image does not have are marked as such, with the reason,
before anything is rented.

**DLoRAL needs a card with 48 GB of VRAM or more** (an L40S or an A100) at
1080p: the VAE decoder's attention at that size wants 16 GB on its own.
A 24 GB card runs out of memory. SeedVR2 is happy on 24 GB.

---

## The presets

The GUI asks one question — **what do you want?** — and fills in the rest:

| Preset | What it does |
|---|---|
| **Enhance** | **The default.** SeedVR2 on a rented GPU: a video diffusion model that runs on the full 1080p and invents the detail the capture lost, frame by frame. De-noised first, sharpened with CAS, a little grain and glow for depth. 1.7 hours and $1.91 for a 72-second 1080p film. |
| **Faithful** | Real-ESRGAN here, 2x, nothing else. Sharp and honest. |
| **Polished** | Real-ESRGAN here, bilateral de-noise, then the blend, soften and grain. The DVD look. |
| **Polished on a rented GPU** | the same, with the AI pass on a card, so your Mac stays cool. Cents. |
| **Regenerate** | DLoRAL on a rented card, one diffusion step, 4x, then the same finishing. |
| **STAR** | the diffusion one with a prompt. The most creative, the least available. |

Everything is still there underneath, in **Advanced — every setting**.

---

## Old footage: fitting it to a size, not a multiplier

Old video is not 1080p and it is not 16:9, and multiplying by 4 does not get
you there. The **Maximum output size** setting takes the size you want
instead of a factor:

> **Maximum output size — the result is fitted inside this**
> `1920x1080 (1080p)` · `1280x720` · `2560x1440` · `3840x2160` · no limit

The app works out the largest size with your video's own aspect ratio that
fits inside the cap, runs the AI at the smallest factor that already covers
it (so the final resize throws nothing away), and then resizes to exactly
the target. A 400x300 clip asked for 1080p comes out as 1700x1080 - not
2560x1440, and not a stretched 1920x1080.

**Crop the black bars** finds the letterbox and cuts it off first, so the
black is not upscaled along with the picture. Old TV transfers and music
videos are usually pillarboxed.

**De-interlace** is automatic: if the frames are flagged interlaced they go
through `w3fdif`, which is the best single-pass deinterlacer in ffmpeg. (The
Topaz recipe uses QTGMC, which needs VapourSynth.) A clip that was
downscaled to 300 lines has no combing left in it, so there is nothing to
do - the app checks rather than assumes.

### A real example

A 2005 TV music video, 400x300, 25 fps, 286 kbps, letterboxed to 400x250,
3 minutes 23 seconds, 5085 frames:

| | |
|---|---|
| letterbox | cropped to 400x250 automatically |
| de-interlace | not needed, the frames are already progressive |
| the AI | `upscayl-realesrgan-x4plus-v3` at 4x → 1600x1000 |
| finished at | **1728x1080** (inside 1080p), HEVC, with the polished look |
| time | 20 minutes on the M1 Pro, about 4.3 frames/s |

The bitrate matters more than the resolution here: at 286 kbps the source is
mostly compression artefacts, and the bilateral de-noise *before* the AI is
what stops them being enlarged into fake detail.

1728x1080 rather than 1920x1080 because the picture is 4:3-ish (1.6:1)
after the crop, and the app keeps the aspect ratio instead of stretching it
to fill the frame.

---

## What a video costs, and why "go all in" is affordable

Measured on the M1 Pro, and priced against the rental cards (29 October
2026). This is your 3 min 23 s music video, 5085 frames:

| Path | On this Mac | On a rented card | Cost |
|---|---|---|---|
| Real-ESRGAN video-tuned x2 | 2 h 27 m | 12 min on a 4090 | **€0.08** |
| Real-ESRGAN x4plus (the deep one) | **57 hours** | 4 h 48 m on a 4090 | **€1.97** |
| DLoRAL, one-step diffusion | CUDA only | 2 h 50 m on an L40S | **€1.60** |

So the deepest model there is, the one that would take **two and a half
days** on your Mac, costs about **two euros** on a rented card. The budget
is never the reason not to use it — the reason was heat and wear, and that
is exactly what renting fixes.

The **Go all in** preset does the deep model on a rented card, and the
progress panel shows the euro cost before you start.

### Which model, and why

From a comparison of ten upscalers on
[r/StableDiffusion](https://www.reddit.com/r/StableDiffusion/comments/1ledzsc/what_is_the_best_video_upscaler_besides_topaz/):

| | rating | the problem |
|---|---|---|
| Topaz | 9/10 | not open source |
| Nomos2 | 8/10 | .pth only - needs PyTorch, not the NCNN path |
| Original | 6.5/10 | |
| **Real-ESRGAN** | **4.5/10** | **plastic skin, and it invents detail in bokeh** |

That 4.5 is the whole reason this tool has a Softness slider and a
video-tuned default:

* **the plastic skin** is what the softness blend undoes - mixing the AI
  result with a plain enlargement puts the original skin texture back;
* **the bokeh** is why the video-tuned `realesr-animevideo` models are the
  default: they were trained on video and do not invent detail where the
  original had none;
* **the flicker** that happens when frames are processed independently is
  avoided three ways here: a video-tuned model, exact frame timing, and the
  source normalised to a constant frame rate before anything else.

The thread also notes that running the `.param`/`.bin` pair instead of the
PyTorch `.pth` checkpoint is about **five times faster** - which is the
path this tool uses. The `.pth` path has a strength slider to tone down
the over-smoothing; the Softness slider here is the equivalent, and it
works with every model.

### The look thread, and the shaders

[NexVid](https://www.reddit.com/r/chrome_extensions/comments/1okqerj/i_made_an_extension_that_let_you_upscale_and/)
is a Chrome extension that upscales and enhances video in real time in the
browser. Its author spells out the recipe: **Anime4K** for the upscale,
**WebGL shaders** for the CRT / VHS / scanline looks, and CSS filters for
contrast, saturation and brightness. Its own advice for the performance
problem is exactly what this tool does: *"mixing sharpen and blur may give
good results if the upscaling is too consuming."*

[WebSR](https://github.com/sb2702/websr) is the interesting version of
that: Anime4K's CNN networks ported to **WebGPU compute shaders**, plus
retrained weights for real-life and 3D footage, with a content-detection
network that switches the model at runtime. It runs a `<video>` element
straight into a canvas.

That is a whole third axis - real-time, in the browser, on the GPU that is
already busy decoding. The reason it is not the default here is that
Anime4K is a sharpening network: it would give exactly the "overtly shiny"
look this tool is built to avoid. It is worth having as a **live preview**
while you decide, not as the record.

### Motion

RIFE (the same ncnn-vulkan shape as the upscaler, so it runs on the Apple
GPU the same way) invents the frames in between, so 25 fps PAL footage
becomes 50 fps and the judder goes away. 4x is two 2x passes.

**Motion** in the settings: off, 2x, 3x or 4x. It runs on the source,
before the upscale, which keeps the pipeline streaming - no pile of 4K
frames on disk. It doubles the frame count, so it doubles the AI cost:
€0.08 becomes €0.16, €1.97 becomes €3.94.

---

## The looks — the "DVD-like" treatment

The **Look** dropdown is separate from the upscaler. It is the treatment from
the [Topaz thread about a DVD-like image](https://community.topazlabs.com/t/how-to-achieve-a-dvd-like-image-quality-look-qtgmc-hybrid-topaz-video-ai/100267/8),
where the recipe is:

1. deinterlace with QTGMC,
2. upscale with **two** models, both with "Improve details and sharpening" at
   about **-30** ("I prefer softer output"),
3. blend them in an NLE — the second one at **65% opacity**,
4. add a little **noise** afterwards, to hide the washed-out patches that
   MPEG compression leaves behind.

The **Enhance** look is the exception: it is for a capture you want
*sharper*, not softer. The soft look below is made by taking detail
**away**, and on a DVD capture that only makes it softer still - so the
Enhance look de-noises first (the compression goes before the diffusion,
not after it) and never softens at all.

The important part is that the soft look is made by taking detail **away**:
a general model upscaled frame by frame gives every frame its own invented
detail, and the picture shimmers. Mixing in a plain, non-invented layer and
*reducing* the sharpening is what stops it.

| Look | What it does |
|---|---|
| **Enhance** | the opposite of the DVD look: de-noise first, keep the AI result (only a fifth of a plain enlargement mixed in, for stable edges), sharpen with CAS, add a little grain and glow. **This is the one for "more detail and more depth".** |
| **Clean** | the AI result on its own. Sharp and detailed, but it invents texture that was never in the picture. |
| **Polished** | denoise first, blend the AI result 65/35 with a plain lanczos enlargement, soften it, add static grain, lift the contrast a little. **This is the DVD-like look.** |
| **Matte** | the same, pushed further: more denoise, more of the plain enlargement, softer still, more grain. Almost no sharpening. |
| **Clean + denoise** | just the denoise, nothing else. |

Two details that matter:

* **The denoise happens before the AI pass.** An upscale amplifies mosquito
  noise and MPEG blocking into "detail". Cleaning first is what stops the
  AI from turning compression artefacts into texture.
* **The grain is static**, the same pattern in every frame. Premiere's Add
  Noise is random per frame, which on moving footage at 50 fps crawls and
  shimmers — the opposite of the calm film look.

Selur's own forum is the other half of this recipe, and it was scraped
without a login: `HYBRID-CHAINS.md` has the processing order he publishes,
a complete Hybrid-generated script for an interlaced DVD upscale, the QTGMC
parameters as he actually sets them, and the plugin set a current QTGMC
should bind. Two things out of it land directly on this pipeline: Hybrid
upscales with **Real-ESRGAN Compact (Anime)** — the same family as our
weights — and his hand-tuned scripts keep the source grain
(`GrainRestore=0.7`, `EZKeepGrain=1.50`) instead of inventing new grain,
which is what the Polished look already does.

---

## Splitting the work: your Mac and a rented GPU

The AI pass is the only part that wants a big GPU. Everything else — the
denoise, the blend, the softening, the grain, the grade and the encode — is
cheap CPU work that ffmpeg does at many times real time on your Mac. So:

```
  YOUR MAC (cool)                    RENTED GPU (a few cents)
  ─────────────                      ────────────────────
  normalise + denoise  ──▶ upload ──▶  Real-ESRGAN / STAR
  blend, soften, grain  ◀── download ──  (the only heavy step)
  grade, encode, audio
```

Your Mac never runs the heavy model, so it stays cool and lasts; the rented
card does the one job it is good at.

### The rented card runs CUDA, not Vulkan

This took a while to find out and it is worth writing down.

The rental boxes hand out a perfectly good CUDA stack — `nvidia-smi` sees
the card, every driver library is there, `torch.cuda.is_available()` is
true. What they do not hand out is a working **Vulkan** ICD. The NVIDIA one
loads, negotiates with the loader, and then returns nothing at all for
`vkCreateInstance`:

```
vkCreateInstance failed -9
invalid gpu device
```

`-9` is `VK_ERROR_INCOMPATIBLE_DRIVER`, which here means the loader found
no usable driver. The NCNN binary is Vulkan-only, so on a rented box it
cannot see the card at all — and no setting of the driver capabilities
changes that (`compute,utility`, `+graphics`, `all` were all tried; the
ICD is found and refused every time). The container's Vulkan loader is
1.3.204 from March 2022, four years older than the driver it is talking
to, and it cannot even parse NVIDIA's layer manifest.

So the rented box runs **the same Real-ESRGAN weights through PyTorch**
(`worker/realesrgan_cuda.py`), and your Mac keeps the NCNN binary, which
works there. Same models, same job, different door. The worker says which
one it is on `/health`:

```json
{"backend": "cuda", "cuda_models": ["realesr-general-x4v3"], ...}
```

The CUDA path does not write PNGs. ffmpeg decodes into a pipe, the model
reads from that pipe, and the result goes into a second pipe that ffmpeg
encodes from. A 300-frame clip used to write and read back about 3 GB of
PNG; now it never touches the disk.

**Measured on an RTX 4090**, not borrowed from a benchmark sheet:

| | |
|---|---|
| 300 frames, 636×480 → 2544×1920 | 20 s of model time |
| the whole job — decode, model, encode | 21 s |
| the whole thing over the proxy | 24 s |
| the same clip on this Mac, NCNN | 109 s of model time |

The compact net does all of its convolutions at the size of the *input*
(only the last one and the pixel shuffle work at the output size), so what
the price follows is the pixels of the input, not the frames. The estimate
uses **4.5 Mpx/s**, measured, and `rental.py` says so in its answer
(`"measured": true`). The old figure of 8 fps came from TensorRT
benchmarks and was wrong by more than a factor of two.

Two more things about the rental boxes, both of which cost time:

- **The DNS is broken.** A pod's resolver answers with IPv6 addresses for
  hosts it has no IPv6 route for, so every HTTPS call fails — `apt`, `pip`,
  `curl`, all of it — while the same commands work on your Mac. The fix is
  two lines at the top of the container:
  `printf 'nameserver 8.8.8.8\nnameserver 1.1.1.1\n' > /etc/resolv.conf`.
  Everything the worker needs is baked into the image, so this only
  matters when something has to be fetched at run time.
- **The SSH proxy carries sessions but not forwarding.** `ssh` into a pod
  works (it insists on a PTY: `-tt`), but `scp` fails with "subsystem
  request failed" and a port forward fails with "unknown channel type:
  unsupported channel type". There is no tunnel to the pod's port 8000, so
  the HTTP path through Cloudflare is the only way in — and Cloudflare
  gives that request a hundred seconds. For a full-length film, use the
  serverless path instead (`ENGINES.md`).

### What it costs

Verified 29 October 2026. Per GPU-hour, on-demand, no minimum:

| Where | Card | $/hour |
|---|---|---|
| RunPod | RTX 4090 / A5000, 24 GB | ~$0.44 |
| RunPod | L40S, 48 GB | ~$0.99 |
| DeepInfra | A100, 80 GB | $1.49 |
| fal | H100, 80 GB | from $1.89 |
| DeepInfra | H100, 80 GB | $2.90 |

DeepInfra bills by the minute with no egress fees; fal bills per second of
GPU time for serverless models.

**The money is not the point — it is cents.** Your 62-second recording is
3023 frames:

| Where | Time | Cost |
|---|---|---|
| this Mac, video-tuned model | ~87 min of GPU work | free, but your machine cooks |
| a rented L40S | ~7 min | **$0.12** |
| this Mac, a deep general model | ~34 hours | free, but not happening |
| a rented L40S, deep general model | ~2.8 hours | ~$2.80 |

The cost of a 4-year-old machine is the heat and the wear, not the
electricity. Moving the heavy step off it is worth doing even if the cloud
were free.

### Setting it up

The worker is one file and a Dockerfile. It speaks a plain HTTP protocol,
so any rental service can front it:

```
  POST {endpoint}/upscale
    body: the video, raw
    headers: X-Scale, X-Model, X-Denoise
    200, the upscaled video as the body
```

```bash
cd ~/upscaler-gui/worker
docker build -t drm-upscaler .
docker run --gpus all -p 8000:8000 drm-upscaler
```

Then in the GUI: **How to upscale → Cloud GPU**, put the box's address in
**GPU worker address**, press **Test the connection**, and **What would it
cost?** before you start.

Where to rent:

* **RunPod** or **Vast.ai** — a pod with the container template above. The
  cheapest by far, and the least to set up. Stop the pod when you are done.
* **DeepInfra** — container rentals (`POST /v1/containers`) or a sandbox you
  `exec` into. Billed by the minute, no egress fees.
* **fal** — a custom deployment; per-second billing, scales to zero.

The image pulls the upstream release at build time (222 MB, once), so it
needs no extra setup beyond the card.

### Testing it without renting anything

The worker runs on your Mac too, which is how the split was tested:

```bash
cd ~/upscaler-gui/worker
UPSCALER_BIN=~/upscaler-gui/vendor/upscaler/bin/mac-amd64 \
UPSCALER_MODELS=~/upscaler-gui/vendor/upscaler/models \
PORT=8791 python3 server.py
```

Then point the GUI's **GPU worker address** at `http://127.0.0.1:8791`.

---

## The second button: STAR

The dropdown at the top of **Settings** switches between two very different
things:

**Real-ESRGAN** (the default) enlarges what is actually in the picture. It
runs here, on the GPU, and it is faithful.

**STAR** is something else entirely. [STAR](https://github.com/NJU-PCALab/STAR)
(ICCV 2025, Nanjing University and ByteDance) is a *diffusion* model: you
give it a short clip and a sentence describing what is in the clip, and it
**regenerates** the clip at a higher resolution — inventing detail that was
never in the source, but keeping that invented detail consistent between
neighbouring frames so it does not shimmer. The prompt is what steers it.

In the simplest terms:

| | Real-ESRGAN | STAR |
|---|---|---|
| What it does | enlarges what is there | invents what could be there |
| Needs a prompt | no | **yes** |
| Runs on | this Mac, on the GPU | a rented NVIDIA GPU, or a free one in the cloud |
| Needs an internet connection | no | **yes** (the cloud route uploads your clip) |
| Flicker | none | low (that is what the model is for) |
| Good for | clean footage, long videos | short, damaged, low quality footage |

### STAR has two routes, and they are not the same thing

| | the **STAR preset** | the **STAR** entry in the dropdown |
|---|---|---|
| Where it runs | a **rented card** (L40S or A100, 48 GB+) | the **Hugging Face Space** |
| Your clip | goes to the card, and nowhere else | **is uploaded to huggingface.co** |
| Costs money | yes — about $0.30 for a two-second clip | no, but the free GPU quota is two minutes a day |
| How long it takes | a few minutes | a queue, if the Space is awake |

The preset is the one to use: the footage stays on the rented box and the
box is destroyed when the job ends. The Space route is the fallback for
footage you are allowed to upload somewhere else.

The important thing to know about STAR either way: **it is not a tool for a
film.** It regenerates a *short clip* — its own example is 320 pixels wide
and two seconds long — and the model wants about 39 GB of VRAM to do it.
That is why the card list for it starts at 48 GB.

### Why STAR cannot run on your Mac

The official code needs CUDA: `torch==2.0.1`, `xformers==0.0.21` (which has
no Apple Silicon build at all) and a GPU with **24 GB of VRAM** — the authors
recommend more, and report ~39 GB for their own small example. Your M1 Pro
has no CUDA and a 16 GB unified memory. There is no port.

So the GUI runs STAR through a **rented card** (the preset) or through the
**official Hugging Face Space** (the dropdown). The Space is a free ZeroGPU
A10G; the client tries the official Space and then two mirrors in turn,
because a free GPU instance can be cold, broken or out of quota.

### …and the free cloud demo does not work either

This is not your fault and it is not a quota you can wait out. From the
Hugging Face docs, ZeroGPU gives an **anonymous visitor 2 minutes of GPU a
day** and a **free account 5 minutes**. One STAR call asks for 100 seconds of
GPU, and the inference takes longer than that.

The Space's own pinned discussion says the same thing, from the authors:

> "Due to the duration limitations of ZeroGPU, the running time may exceed
> the allocated GPU duration. If you'd like to give it a try, you can
> duplicate the demo and assign a paid GPU for extended use."
>
> — Rui Xie, author

> "Since the inference time will exceed the ZeroGPU limitation, you could try
> to duplicate the updated version running with your paid GPU"
>
> — Yinhong Liu, author

Users report it failing *even on a paid single GPU*; the one configuration
reported working is **1x L40S + 1x L4**. The mirror Spaces fare no better:
`O-ken5481/STAR-UpSucale` is in `RUNTIME_ERROR`, and
`multimodalart/STAR-zerogpu` rejects jobs the same way.

So the integration is correct and complete — clip preparation, upload, run,
download, the lot — but **the far end is the broken part.** The GUI tells you
this before it uploads anything, and shows which spaces are alive.

### The route that does work: a rented GPU

`star_remote.sh` does the two halves:

```bash
# on your Mac: cut the segment and shrink it to what STAR can handle
./star_remote.sh pack ~/drm-capture/out/mne-play-capture-60s.webm clip.mp4 20 2 320

# on a rented NVIDIA box (RunPod / Lambda / Vast.ai, or Colab with an L4/A100):
./star_remote.sh run clip.mp4 "A television studio broadcast, a presenter talking to camera"
```

The remote box wants an NVIDIA card with 24 GB+ (40 GB is safer), CUDA 11.8+,
conda, and ~30 GB of disk for the weights. The script clones STAR, downloads
the weights once, and runs the official inference.

The GUI's STAR panel produces exactly the same clip file, so you can prepare
it in the GUI and then run it remotely.

### What that means in practice

* **Your clip is uploaded to huggingface.co.** The GUI says so in red before
  you press the button. Only do this with footage you are allowed to upload.
* **The input has to be small.** The Space's own example is 320x240 at 25
  fps. The GUI cuts the segment you pick, scales it to the width you choose
  and sends that. You get back a clip at `width x upscale`.
* **It is slow and quota-limited.** The GPU budget is 100 seconds per call.
  If a space is out of quota the GUI says which spaces are alive and which
  are not, rather than failing silently.

### The settings

| Field | Meaning |
|---|---|
| **Prompt** | what is in the clip, in a sentence. It steers what the model invents. |
| **Start / Length** | which part of the source to send, in seconds. |
| **Input width** | how wide the uploaded clip is. Smaller = faster and more likely to fit in the GPU budget. |
| **Upscale** | 2x or 4x. STAR was trained for 4x. |

---

## Choosing a model

Measured on this machine: 3 frames of 1080p, upscaled to 4K, one process for
the whole set. The "network depth" is the line count of the model's `.param`
file, which is what the run time is actually made of.

| Model | s/frame | Depth | Notes |
|---|---|---|---|
| `xintao-realesr-animevideo-x2-v3` | **1.73** | 43 | **The one to use for video.** Trained on video: temporally stable, flickers least. 2x only. |
| `xintao-realesr-animevideo-x3-v3` | 2.41 | 43 | Same, 3x → 5760×3240 |
| `xintao-realesr-animevideo-x4-v3` | 3.52 | 42 | Same, 4x → 7680×4320 |
| `upscayl-realesrgan-x4plus-v3` | **2.68** | 74 | **Best general-purpose model.** Upscayl's tuned RealESRGAN — fast *and* general. |
| `upscayl-realesrgan-x4plus-anime` | 13.51 | 270 | Animation only. |
| `xintao-realesrgan-x4plus-anime` | 15.03 | 270 | Animation only. |
| `xintao-realesrgan-x4plus` | 40.73 | 1001 | The classic general model. 24x the work of the video models. |
| `upscayl-realesrgan-x4plus` | 41.08 | 1001 | General, but 15x the work of the video models. |
| `upscayl-remacri` | 41.18 | 1372 | Gentler, better at keeping fine texture. Very deep. |
| `upscayl-ultramix_balanced` | 42.07 | 1372 | Balanced general model. Very deep. |
| `upscayl-ultrasharp-v2` | 45.22 | 1001 | General purpose. |
| `upscayl-ultrasharp` | 46.10 | 1372 | General purpose. The deepest, the slowest. |

**Two rules that matter more than the model choice:**

1. **Use the scale the network was trained for.** Every model here except the
   three video ones is an x4 network. Ask it for 2x and it still runs the
   full x4 network and then downscales the result — same time, no benefit.
   The GUI shows each model's native scale and picks it for you.

2. **For broadcast TV, use the video-tuned model.** A general model applied
   frame by frame gives every frame its own invented detail, and the result
   shimmers. The `realesr-animevideo` models were trained on consecutive
   frames and stay put.

**And be careful with how much you upscale.** 1080p broadcast video has no
real detail above its own resolution. Going to 4K makes the picture bigger,
not better, and any AI model will invent texture that was never there. If
the goal is to watch on a bigger screen, a good scaler (`lanczos`) plus a
light unsharp is often closer to the original than Real-ESRGAN is.

---

## How long does it take?

Real-ESRGAN is heavy. On this M1 Pro, a 1080p frame costs 1.7 s with the
video-tuned x2 model and 41-46 s with the deep general models. A 60 second
50 fps clip is 3000 frames, so:

| Clip | video-tuned x2 (1.73 s/frame) | a deep general model (41 s/frame) |
|---|---|---|
| 3 s (150 frames) | ~4 min | ~1 h 45 min |
| 60 s (3000 frames) | ~87 min | ~34 hours |

The progress bar shows the measured rate and a live ETA, so you know what
you are in for before committing. **A short clip is the sensible way to try
a model out.**

---

## Disk and memory

* The GUI keeps only one chunk of frames on disk at a time (30 frames by
  default), so the peak is a few hundred megabytes, not tens of gigabytes.
* The output file is the big thing: a 4K H.264 encode of a minute of TV is
  a few hundred megabytes.
* The upstream script wants 3x the video size free. This one needs much
  less.

### When the disk runs out: the scratch space goes onto your iCloud Drive

The scratch space is `~/upscaler-gui/work` — the uploads, the job folders
and the finished videos. A big film puts a couple of gigabytes in there,
and when the disk fills, a job dies halfway through with "no space left on
device".

So there is a guard. Before a job starts it looks at the free space on the
volume, and if there is less than **10 GB** left it moves the scratch space
into your iCloud Drive:

```bash
python3 tools/space_guard.py            # what is going on
python3 tools/space_guard.py relocate   # do it now
python3 tools/space_guard.py restore    # put it back
```

It leaves a symlink behind, so every path in the project keeps working
unchanged — the GUI, the uploads, the finished videos, all of it.

What it is and what it is not, because this is easy to get wrong:

* **It is a pressure valve, not a spare disk.** The iCloud container is on
  the same volume, so a file there takes local space until macOS reclaims
  it. With "Optimize Mac Storage" on (it is, on this Mac), macOS evicts
  iCloud Drive files when the disk gets tight and leaves a placeholder
  behind. Reading one afterwards downloads it again.
* **There is no way to evict on demand.** `brctl evict` refuses — "The
  file cannot be evicted" — and `fileproviderctl` has no evict verb at
  all. So the guard does not try: it puts the scratch where macOS can
  reach it, and lets macOS do the reclaiming when the disk fills up.
* **It costs a little of your iCloud.** The scratch is uploaded to your
  iCloud, so it takes bandwidth and a little of your quota. It is scratch
  space, so it is thrown away afterwards — but the upload is real.
* **A file that is open while macOS evicts it keeps working** — the open
  descriptor holds the data. A file that is read again afterwards is
  downloaded again, which is slow but works.

The proof that the reclaiming works is in your own iCloud Drive: files over
900 MB that read as 0 B locally, because they are placeholders.

---

## Files it will show you

`~/Videos`, `~/Movies`, `~/Downloads`, `~/Desktop`, `~/drm-capture/out`, and
anything you drop on the page (which is copied into `work/uploads/`). The
server refuses to read anything outside those folders.

---

## Legal

Real-ESRGAN and the models are BSD-3-Clause / BSD-2-Clause, from
[XinTao's Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan) and
[Upscayl](https://github.com/upscayl/upscayl). Upscaling a video you do not
own may be unlawful where you live.
