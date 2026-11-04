# The engine stack

One protocol, four engines. The GUI does not know which one is running; the
worker does.

```
  Mac (always)                         rented GPU (only when "rent it" is on)
  ----------------------------------   ------------------------------------
  crop the letterbox                      the AI pass, and nothing else
  de-interlace
  de-noise (hqdn3d or bilateral)
  pre-scale to the target
  ---- upload the video ---->             POST /upscale  + X-Engine
  <---- the upscaled video ----           POST /upscale  -> video
  blend with a plain enlargement
  soften, grain, grade
  encode
```

The finishing half stays on the Mac on purpose. It is CPU work that ffmpeg
does at many times real time, so it costs nothing, it keeps this machine
cool, and a crashed remote job only costs the job.

Selur's Hybrid does the same split of work, and its published filter order
matches this one: deinterlace first, denoise after, grain handling late,
resize near the end. `HYBRID-CHAINS.md` has his order, a complete
Hybrid-generated script for an interlaced DVD upscale, and the QTGMC
parameters he actually uses - recovered from forum.selur.net without a
login, with `tools/selur_fetch.py` doing the crawling.

## The engines

| Engine | What it is | Needs | Where it can run |
| --- | --- | --- | --- |
| `realesrgan` | Real-ESRGAN / AnimeVideo over NCNN-Vulkan | a Vulkan GPU | this Mac (M1 Pro) |
| `realesrgan` (CUDA) | the same weights, over PyTorch | a CUDA GPU | a rented NVIDIA card |
| `seedvr2` | SeedVR2, one-step diffusion video SR | torch + CUDA or MPS | a rented card, or this Mac through MPS (slow) |
| `star` | STAR, text-prompt-guided video SR | torch 2.0.1, CUDA, about 39 GB | a rented card (H100 or A100 80 GB) |
| `kandinsky6` | Kandinsky 6.0 Video SR, 2-step distilled | torch + CUDA, about 20 GB | a rented card (H100 for speed) |
| `dloral` | DLoRAL, one-step diffusion video SR | torch 2.0.1, CUDA | a rented card only |

`realesrgan` is the fast path and the default. The diffusion engines cost
roughly two to five times as much per clip and are worth it on shots that
need re-synthesised texture, not on a whole video.

## What the image can do, and what it cannot

The diffusion engines are **opt-in layers** in the image (`BUILD_DLORAL=1`,
`BUILD_SEEDVR2=1`, `BUILD_STAR=1`), and the image that is pushed by default
has none of them: the only engine in use is `realesrgan`.

That fact used to be invisible. The worker answered `/health` with all four
engines no matter what was in it, so a job asking for DLoRAL was told yes,
rented a card, ran the denoise, booted a pod, and only then discovered that
the engine was not there. The job now refuses **before** anything is spent:

```
the image docker.io/bzz1/upscaler:latest cannot run the dloral engine -
it has: realesrgan, face. That engine is an opt-in layer in the image:
build it with --build-arg BUILD_DLORAL=1 and point the GUI at that image,
or use the Polished preset, which the default image can run.
```

There are two guards. The project keeps the list for the images it pushes
(`rental.IMAGE_ENGINES`), which is free and instant; and the image carries
its own list in `/app/engines.json` and answers it on `/health`, so the
worker has the last word for an image the project has never seen.

## The face route (not used)

The image carries a second route, `POST /face`, which repairs the faces with
RestoreFormer (TencentARC/GFPGAN v1.3.4). It is **not offered and not to be
used**: it was tried on the music video and the result looked unnatural next
to the Topaz reference, so the face pass is off. The code and the weights
stay in the image because the image is already built and rebuilding it for
the sake of removing them would cost more than it saves; nothing in the GUI
offers it, and nothing calls it.

## The rented card runs CUDA, not Vulkan

This is the one thing that is not obvious.

The rental boxes hand out a working CUDA stack - `nvidia-smi` sees the card,
every driver library is there, `torch.cuda.is_available()` is true. What they
do not hand out is a working **Vulkan** ICD. The NVIDIA one loads, negotiates
with the loader, and then returns nothing for `vkCreateInstance`:

```
vkCreateInstance failed -9
invalid gpu device
```

`-9` is `VK_ERROR_INCOMPATIBLE_DRIVER`, which here means the loader found
no usable driver at all. The NCNN binary is Vulkan-only, so on a rented box
it cannot see the card at all, however the driver capabilities are set
(`compute,utility`, `+graphics`, `all` make no difference - the ICD is found
and refused either way). The container's Vulkan loader is 1.3.204 from March
2022, four years older than the driver it is talking to, and it cannot even
parse NVIDIA's layer manifest.

So the rented box runs **the same Real-ESRGAN weights through PyTorch**
(`worker/realesrgan_cuda.py`), and the laptop keeps the NCNN binary, which
works there. Same models, same job, different door. The worker tells you
which one it is on `/health`:

```json
{"backend": "cuda", "cuda_models": ["realesr-general-x4v3"], ...}
```

The CUDA path does not write PNGs. ffmpeg decodes into a pipe, the model
reads from that pipe, and the result goes into a second pipe that ffmpeg
encodes from. A 300-frame clip used to write and read back about 3 GB of
PNG; now it never touches the disk.

### Measured, not borrowed

On an RTX 4090 (Runpod, driver 595.91.07, torch 2.8 + cu128):

| | |
| --- | --- |
| 300 frames, 636x480 to 2544x1920 | 20 s of model time |
| the whole job - decode, model, encode | 21 s |
| the whole thing over the proxy | 24 s |
| the same clip on this Mac, NCNN | 109 s of model time |

The compact net does all of its convolutions at the size of the *input*
(only the last one and the pixel shuffle work at the output size), so what
the price follows is the pixels of the input, not the frames. The estimate
uses **4.5 Mpx/s**, measured, and `rental.py` says so in the output
(`"measured": true`). The old figure of 8 fps was borrowed from TensorRT
benchmarks and was wrong by more than a factor of two.

## The finishing pass, and the white highlights it used to leave

The finishing half is the local one: the blend with a plain enlargement, the
softening, the grain, the halation, the grade and the encode. It runs on this
Mac because ffmpeg does it at many times real time, and it costs nothing.

It had four bugs, all of which only showed up on a **full-length** video, and
all of which are worth writing down because each one is a trap:

| what | what it did |
| --- | --- |
| `prepare_upload` | referred to a name that only existed inside another function, so every cloud upload died with `NameError` before it started |
| the perlin grain | came from an endless lavfi stream with no `-shortest`, so the encoder waited for it forever and the job never finished |
| `halation_sigma` | used the picture's height before the picture had been probed |
| `server.py` | checked `rent_approved` at the door and then dropped it, so no job could ever get as far as renting anything |

And then the one that actually mattered, because it made the picture wrong
rather than the job fail:

**The halation subtracted.** The cloud path blended a blurred copy of the
picture back in arithmetically, `A + (B - 128) * 0.07`, which is a *subtraction*
below mid-grey. It crushed the shadows, and a crushed shadow is not the end of
it: the grain step afterwards added its own offset on top of those dead
pixels, and the highlights went to a flat, dead white. On the 3 min 23 s
video it ate 13.6% of the picture - big white blobs over the face in the
opening scene.

The local path had already solved this, and the cloud path had not been
brought in line with it. The recipe that works is the local one:

- **the halation only ever adds**: threshold the highlights, blur them, and
  add them back with a floor at zero, `A + max(0, B - 128) * gain`, so the
  shadows are untouched;
- **the grain is measured and subtracted**: the perlin pattern has a mean of
  its own - about 149 at these settings, not 128 - so the field is rendered
  to a file, its mean is measured over the *whole* file, and that is
  subtracted;
- **the result is kept inside the legal range**, `min(235, max(16, ...))`, so
  a grain that lands on a picture that is already near white cannot clip it.

With those three, the count of blown-white pixels over the whole film went
from 13.6% to 0.000%.

The lesson for the split: because the AI pass and the finishing pass are
separate, a broken finishing pass can be re-run on the saved AI output for
**nothing**. The GPU work was already done and paid for; only the Mac had to
do the work again.

## Three things about the rental boxes
**The DNS is broken.** A pod's resolver answers with IPv6 addresses for
hosts it has no IPv6 route for, so every HTTPS call fails - `apt`, `pip`,
`curl`, all of it - while the same commands work on the laptop. The fix is
two lines at the start of the container:

```bash
printf 'nameserver 8.8.8.8\nnameserver 1.1.1.1\n' > /etc/resolv.conf
```

Everything the worker needs is baked into the image, so this only matters
when something has to be fetched at run time.

**The SSH proxy carries sessions but not forwarding.** `ssh` into a pod
works (it insists on a PTY: `-tt`), but `scp` fails with "subsystem request
failed" and a port forward fails with "unknown channel type: unsupported
channel type". There is no tunnel to the pod's port 8000, so the HTTP path
through Cloudflare is the only way in - and Cloudflare gives that request a
hundred seconds.

**An estimate nobody can feel is an estimate nobody checks.** The approval
dialog used to say "about 168441s of AI work and 4 min of pod start-up" for
a job that would have run for **47 hours** on the card, next to "about
$51.03". The seconds were right and the dollars were right, and the run was
approved anyway, because a number in seconds reads as a number and "47
hours" reads as a decision.

So the duration is now in words ("47 h 00 m", "2 days 3 h"), an estimate
that is a **guess** says so, and anything over an hour gets a second
sentence about it. The engines that have never been measured - DLoRAL,
STAR, SeedVR2 - are the ones with the wild numbers, and the guess table
itself is a table of guesses:

```python
GPU_FPS = {
    "realesrgan": 8.0,   # measured: 4.5 Mpx/s of input pixels
    "seedvr2": 1.0,      # a guess
    "star": 0.15,        # a guess
    "dloral": 0.145,     # a guess
}
```

## The prompt

STAR is the odd one out: it rebuilds the missing detail with a text-to-video
prior, so the prompt decides *what gets invented*. The "polished" preset is a
fixed filter recipe; STAR makes the same decision a sentence you write.

It travels as one header: `X-Prompt`. In the GUI it is the "Direction" box
under the engine list, and the STAR preset fills it with a starting point
(`1990s DVD player, soft analog bloom, gentle film grain, slightly faded
colour`). The weight is a single line, `light_deg.pt`, from
<https://huggingface.co/SherryX/STAR>, so the image builds without a manual
download. STAR wants about 39 GB of VRAM for a 4x upscale of 72 frames; lower
`STAR_CHUNK` for a smaller card.

The recipe this belongs to, from the people who chase the same look: Hybrid
QTGMC for the de-interlace and the denoise (KNLMeansCL, FFV1 lossless), then
SeedVR2 for the upscale on a 16 GB card or better, then a final Topaz Video
AI pass with Iris, Proteus or Rhea to bring it to FullHD, and an NLE to
combine. Their consensus was to downscale to roughly 448x252 (16:9) or
422x308 (4:3) before a 3x SeedVR2 pass, which lands on 1152x864 or 1266x924
and leaves the last stretch to the sharper model.

Upstream projects:

- SeedVR2: <https://github.com/ByteDance-Seed/SeedVR> (the ComfyUI release
  with the standalone CLI and the Apple-Silicon backend is
  <https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler>)
- STAR: <https://github.com/NJU-PCALab/STAR> (weights at
  <https://huggingface.co/SherryX/STAR>)
- Kandinsky 6.0 Video SR: <https://github.com/kandinskylab/kandinsky-6-sr>
  (a Diffusers bundle and a `kandy-sr` CLI)
- DLoRAL: <https://github.com/yjsunnn/DLoRAL>
- video2x: <https://github.com/k4yt3x/video2x> (Real-ESRGAN, RIFE and Anime4K
  in one container, published on GitHub Container Registry, so a rented box
  can pull it without anyone building an image first)

## The native-scale rule

The NCNN Real-ESRGAN binary only tiles correctly at the scale its model was
trained at. Given any other `-s` it emulates that factor by tiling the
input, and the tiles do not line up: the output comes back as a patchwork of
blocks at different magnifications, which looks like a ghosted double image
rather than an obvious error.

So the model always runs at its own factor, and the input is pre-scaled
instead. That lands the result on the target size exactly, and it is less
work for the GPU: a 584x400 frame aimed at 1576x1080 becomes 394x270 at 4x
instead of 584x400 at 3x.

`native_scale_for()` in `engine.py` and `NATIVE_SCALES` in
`worker/server.py` hold the table. If a new model is added, its native
factor has to go into both.

## Adding an engine

1. Add a function to `worker/server.py` that takes a video path and writes a
   video path, and route it in `do_POST` on `X-Engine`.
2. Add the dependency to `worker/Dockerfile` behind a build argument, so
   the fast image stays small.
3. Add the name to `rental.py`'s engine-to-GPU mapping, so a diffusion
   engine asks for a card with enough memory.
4. Add the name to `cloud.py`'s cost estimate, so the GUI can tell the
   truth about what a job will cost.

Nothing else changes: the finishing half, the GUI and the rental lifecycle
are the same for every engine.

## The two diffusion engines

Both are one-step diffusion video super-resolution: they invent detail, and
they invent it *consistently* from frame to frame, which is what a diffusion
does that a per-frame upscaler cannot.

| | SeedVR2 | DLoRAL |
| --- | --- | --- |
| where | ByteDance Seed, the official ComfyUI release | yjsunnn, NeurIPS 2025 |
| size | 3B transformer (7B also exists) | the Stable Diffusion 2.1 UNet |
| weights | 3.4 GB (FP8) on Hugging Face | 11 GB on Hugging Face |
| precision | FP16, FP8, GGUF Q4/Q8 | FP16 |
| speed-ups | `torch.compile` (20-40% on the DiT), Flash Attention 2, GGUF | - |
| output | any resolution, and it restores at 1x | 4x, from a small canvas |
| stack | the worker's own torch, nothing extra | its own venv: torch 2.0.1, diffusers 0.25, mmcv-full |

### What DLoRAL needs before it will run at all

Three things were missing, and each of them failed on the card, where
finding out is the most expensive way to find out:

* **setuptools.** mmcv 1.7.2 reaches for `pkg_resources`, which setuptools
  removed in version 81. The venv gets the newest setuptools by default,
  so `import mmcv` fails. Pinned to `setuptools==69.5.1`.
* **xformers.** DLoRAL's own inference path asks for memory-efficient
  attention - `unet.enable_xformers_memory_efficient_attention()`, and its
  VAE hook imports `xformers.ops` directly. Its requirements.txt asks for
  `xformers==0.0.20`, which is the build that goes with torch 2.0.1.
* **the card.** At 1080p the VAE decoder's attention wants 16 GB on its
  own, and a 24 GB card runs out of memory with
  `Tried to allocate 7.81 GiB`. DLoRAL's VAE hook has a memory-efficient
  branch, but it is switched off upstream (`elif False:`) and it is
  written for the old ldm attention blocks, not for the diffusers 2.1 VAE
  that DLoRAL actually loads. So the route is: **48 GB of VRAM or more for
  1080p** - an L40S or an A100. A 720p result is a quarter of the pixels
  and fits a 24 GB card (that one is worked out from the attention size
  rather than measured, but it is the number the card list and the
  estimate use).

### The frames it makes, and the frames it reads
The DLoRAL script writes **two** sets of frames:

* the ones it **read**, next to the video it read them from - that is the
  canvas, untouched;
* the ones it **made**, in the output directory, under the video's name.

The job was picking up the first set. The run took as long as it should,
the worker reported success, the pieces came back, the video was joined
and the finishing pass ran - and the result was the canvas with a new
bitrate. The diffusion had been thrown away, and nothing said so.

It is the kind of bug that a green log hides completely: the only way to
see it is to look at the size of the result and ask whether it is the
size the model was asked for.

### The cost follows the output, not the input

This is the whole answer to "can we make it cheaper". The transformer works
at the resolution of the picture it is producing, so:

| what it produces | pixels | how it compares |
| --- | --- | --- |
| 480p | 0.41 Mpx | about a fifth of 1080p |
| 1080p | 2.07 Mpx | five times the work of 480p |

So a **480p restore followed by a cheap upscale** is the affordable way to
get the diffusion's look at 1080p. The upscale afterwards is Real-ESRGAN,
whose cost follows the *input* pixels, so building 480p up to 1080p costs a
couple of minutes of card time.

SeedVR2 makes this easy because it restores at 1x: give it a 480p picture
and it hands back a clean, repaired 480p picture, and the upscale from
there is the ordinary part of the pipeline. DLoRAL is a 4x model, so the
same trick means starting from a 120p canvas, which is a lot of nothing to
work with.

### What it actually costs, measured

The table above is the arithmetic. The measurement is here, and it is not
the same arithmetic - the picture that goes in costs something too, because
the VAE encodes the frames it is given.

SeedVR2, on an RTX 4090 (Runpod, $0.89/h), on the real film: the same 25
frames three ways, each timed twice so that the model load and the one-off
costs cancel out.

| what goes in | what comes out | a frame | the whole 72 s film |
| --- | --- | --- | --- |
| 1080p | 1080p | 1.68 s | 1.7 h - **$1.50** |
| 1080p | 480p | 0.96 s | 1.0 h - **$0.85** |
| 480p | 480p | 0.68 s | 0.7 h - **$0.60** |

Three numbers, two things to read out of them:

* **the output pixels set the price, and the input pixels set it too.** A
  480p restore fed with the 1080p original costs half again as much as the
  same restore fed with a 480p picture. So the cheap route is not "restore
  at 480p" but "**downscale to 480p first, then restore at 480p**".
* **480p is a half, not a fifth.** The output pixels do a fifth of the work,
  but the input is still encoded at its own size, so the whole thing is
  about 2.5 times cheaper rather than 5.

For comparison, the Real-ESRGAN route that produced the delivered film -
denoise, 540p canvas, WDN 4x on the card, back to 1080p - cost **$0.21**
for the same 72 seconds. So the order is:

| route | the whole film |
| --- | --- |
| Real-ESRGAN WDN (what was delivered) | **$0.21** |
| SeedVR2 at 480p, from a 480p canvas | $0.60 |
| SeedVR2 at 480p, from the 1080p original | $0.85 |
| SeedVR2 at 1080p | $1.50 |
| DLoRAL at 4x to 480p | $1.01 |
| DLoRAL at 4x to 1080p | about $5 (extrapolated) |

The diffusion is not the cheapest way to fix a bad bitrate - it is the
best-looking way, and it is expensive. It is worth it on a shot, not on a
whole film, and that is exactly what the price says.

### DLoRAL, measured

DLoRAL is a 4x model, so it works from a small canvas. Measured on an
RTX 4090, the same way: **1.14 s a frame** for 4x to 480p from a 120p
canvas - **$1.01** for the film.

The 1080p route is **not** measured, and the reason is a card rather than
a time: the VAE decoder's attention at 1080p wants 16 GB on its own and a
24 GB card runs out of memory, so it needs an L40S or an A100. The cost
follows the output pixels, and 1080p is five times 480p, so it lands at
about **$5** for the film. That is an extrapolation from the measured
480p number, not a measurement, and it is priced that way in the
estimate.

Two things are worth knowing about DLoRAL before spending a card on it:

* it is **not** the cheap version of the diffusion route. At the same
  output it costs about twice what SeedVR2 costs, and at 1080p it needs
  a bigger card as well.
* the canvas it works from is small. A 120p canvas for a 480p result is a
  lot of nothing to invent detail from, and the model has to be
  consistent across the frames - which is the whole point of it.

The model behind the estimate, from those measurements:

```
SeedVR2:  0.43 s a frame, + 0.43 s per Mpx of output, + 0.17 s per Mpx of input
DLoRAL:   0.11 s a frame, + 2.40 s per Mpx of output
```

The estimate used to price a diffusion by the input pixels only, with a
guessed rate that was three times too slow. It made a 480p restore look
like a 1080p one. It is measured now.

### Measuring a diffusion, and the twenty-second trap

The Mac asks the worker how far the job has got every **twenty seconds**.
That is fine for a job that takes hours. It is useless for a measurement:
a run that really took five seconds and a run that really took nineteen
both come back on the next poll, and the two look the same. The first
version of the measurement reported the same number for a 5-frame run and
a 25-frame run at 480p, and the film came out at $0.05 - which was the
poll interval, not the work.

So a measurement polls fast (1.5 s), and it takes **three** timings at each
resolution:

1. five frames - thrown away. The first run at a resolution pays a one-off
   cost of its own: the kernels are compiled for that shape and cuDNN picks
   its algorithms.
2. five frames - timed.
3. twenty-five frames - timed.

The per-frame time comes from 2 and 3, so the model load cancels out. The
run that paid the download of the weights (3.4 GB) is the one in step 1,
so the download is out of the measurement too.

### STAR: the steerable one, and the two ways to run it

STAR is not an enlargement. It regenerates a **short clip** from a sentence
describing what is in it - its own example is 320 pixels wide and two
seconds long - and it wants about 39 GB of VRAM to do it. It is a tool for
a shot, not for a film.

There are two routes, and they are not the same thing:

| | the STAR preset | the STAR entry in the dropdown |
|---|---|---|
| Where it runs | a **rented card** (L40S or A100) | the **Hugging Face Space** |
| The clip | goes to the card, and nowhere else | **is uploaded to huggingface.co** |
| Costs | about $0.30 for a two-second clip | free, but the free GPU quota is two minutes a day |

The free Space route is a dead end without a paid HF GPU allocation:
ZeroGPU gives an anonymous visitor two minutes of GPU a day and a free
account five, one STAR call asks for 100 seconds, and the authors say the
inference takes longer than that. The mirrors are broken too
(`O-ken5481/STAR-UpSucale` is in RUNTIME_ERROR). So the preset rents a
card, and the Space route stays in the dropdown for footage that may be
uploaded somewhere else.

The rented route needed three things that were missing:

* **a venv.** STAR's requirements ask for `torch==2.0.1`, and installing
  that over the image's torch 2.5.1 would take the Real-ESRGAN, SeedVR2
  and DLoRAL layers down with it. So STAR gets a venv of its own, like
  DLoRAL.
* **a route in the worker.** There was none: the preset asked for an
  engine the worker did not have, on an image that did not have it. The
  worker now has `POST /star` (a job id, because the inference takes
  longer than the proxy's hundred seconds) and `GET /star/<id>`.
* **the panel's fields.** The GUI sent `star_start`, `star_seconds`,
  `star_width` and `star_upscale` only for the Space route, so the rented
  route used the defaults whatever the panel said.

The clip is cut and shrunk **on the Mac**, before it goes up: STAR is not
given the film, it is given the two seconds the panel asks for.

Two more things that only showed up on the card:

* **the image each engine pulls.** The rental picks the image by the
  engine, and only DLoRAL and STAR had their own. SeedVR2 was pulling the
  plain image - which has no SeedVR2 in it - so the job failed on the
  card with "the worker cannot run the seedvr2 engine", after the money
  was spent and the image was pulled.
* **the job has to keep its own copy of the upload.** The worker hands the
  upload over in a temporary directory and cleans it up the moment it
  answers with the job id, so a job that reads the original finds
  nothing. STAR stopped with "stack expects a non-empty TensorList".

Verified on the card on 2026-10-31: a 320x240, two-second clip went up,
and STAR returned **1280x960** at the frame rate it went up with - a real
4x of the picture, and the first time this route had produced anything
but the input.

### The card lifecycle: rent, use, give back

The card is created when the job starts and given back when it ends, in a
`finally`, so a job that fails or is stopped still returns it. An orphaned
pod bills by the hour and nobody notices.

The three ways of giving a card back, and what each one costs:

| | what happens | what it saves | what it costs |
|---|---|---|---|
| terminate | the pod is deleted, disk and all | nothing | the next run downloads the weights again |
| stop | the GPU is released, the disk is kept | the next run of the same engine finds the weights on the card | free: a stopped pod with no volume disk of its own bills nothing |
| a network volume | the weights live on a volume that belongs to no pod | every pod finds them, and they survive a termination | $0.07/GB/month, and the card has to be rented in the volume's data centre |

The weights are what makes this matter: DLoRAL's are 11.8 GB, SeedVR2's
3.4 GB and STAR's 7.6 GB. How long the fetch takes is not a fixed
number: one pod pulled DLoRAL's eleven gigabytes at 72 MB/s (2 min 43 s,
$0.05) and another managed 5-9 MB/s, which would be $0.79.

So the network volume buys certainty, at $2.80 a month, and it only
pays for itself at about thirty runs a month. **It is off by default**
for that reason, and the card that is kept warm between jobs covers the
same ground for nothing. Turn it on with:

```
python3 tools/volume.py create --dc US-TX-3 --size 40
# and put the id it prints into RUNPOD_VOLUME_ID
```

With the volume off, the card can be rented in any data centre - which
is worth something on its own: a centre that has an L40S at 11:00 may
have none at 11:05, and the job does not have to wait for it.

Where the weights go depends on the pod:

* **the container disk** (`/app/...`) is wiped when a pod is terminated,
  and it is what an image carries;
* **the volume disk** (`/workspace`) survives a stop;
* **a network volume** survives a stop *and* a termination, and is shared
  between pods.

The entrypoint points the weight directories at the volume disk when there
is one, so the same paths work either way. The Dockerfile can bake the
weights into the image instead, which trades a bigger pull on every run
for a much faster one: the image comes from Runpod's CDN at 50 MB/s, the
weights from Hugging Face at 5-9 MB/s.

### What it cannot do inside a request

Both engines take hours over a film, and the pod's proxy gives a request
about a hundred seconds. So the worker takes the video, answers with a job
id, and does the work in the background; the Mac polls and downloads the
pieces one at a time, because the answer for a whole film would be far too
big for one response.

`POST /diffusion` starts the job, `GET /diffusion/<id>` polls it and
`GET /diffusion/<id>/<piece>` fetches a finished piece.

### Where the SeedVR2 CLI puts its output

The CLI names the file it writes after the **input**, and which name it uses
depends on how it was told to write:

| how it was called | what it writes |
| --- | --- |
| `--output <dir>` | `<dir>/<input name>.mp4` - the same name |
| no `--output` | `<input>_upscaled.mp4`, next to the input |

The worker always passes `--output`, so the file comes back as
`piece-00000.mp4` and not as `piece-00000_upscaled.mp4`. The job looks for
both names. Looking for only one of them makes every piece report "no
output", and the failure only shows up after the model has already run -
which on a rented card is money.

Two smaller things about the same call:

* the writer is `--video_backend ffmpeg`, not OpenCV's. OpenCV's `mp4v`
  writer needs codec support a headless build does not always have, and the
  worker's own SeedVR2 route already uses the ffmpeg one.
* the weights are fetched on the card, so the **first** job on a cold box
  pays for the download (3.4 GB) and the model load. A measurement that
  times the first job is measuring the download. Warm the model up first,
  on a five frame clip, and time the second one.

### The versions are pinned, and they have to be

`transformers` 5.x needs a newer torch than the one the worker runs, and
`diffusers` then fails at import time inside the transformer. The image
pins the pair the SeedVR2 release was built against:

```
diffusers==0.35.1  transformers==4.55.2  peft==0.17.1  accelerate==1.10.1
```

The weights are fetched **on the card**, not baked into the image: eleven
gigabytes would make the image enormous and the push slow, and a rented box
has the bandwidth and the disk for it. The image's entrypoint also fixes the
pod's resolver, which otherwise answers with IPv6 addresses for hosts it has
no IPv6 route for - and the weights come over HTTPS.

## Long videos: the serverless path

A pod goes through Cloudflare, and Cloudflare gives that request a hundred
seconds. At the measured speed that is about 1,500 frames of 1080p - twelve
seconds of a film. It is enough for a clip and not enough for a film, and
there is no tunnel around it (see above).

So a full-length video goes through a **serverless endpoint** instead. It is
the same worker, with a different door: the job arrives as JSON and the
answer leaves as JSON, so nothing limits how long a job may run.

### Or in pieces, through the pod

The serverless route needs either a public URL for the input or a bucket for
the output, and neither is set up. The way to get a whole film through the
pod route anyway is to **send it in pieces**.

`cloud.upscale_chunked()` cuts the prepared video on frame boundaries (a
filter, not a seek, so nothing is lost or repeated at the seams), sends each
piece as its own request, and joins the answers back together. The AI pass
is frame by frame, so the pieces are independent and no seam shows where two
of them meet.

The piece size is the whole trick: 450 frames is about forty seconds of work
on the card, which leaves plenty of room inside Cloudflare's hundred.

The card is paid for by the minute and the spin-up is paid once per session,
so a dozen pieces cost barely more than one long job would. Measured on the
3 min 23 s video (`u6jOqRpwHhA`, 636x480, 5257 frames):

| | |
| --- | --- |
| pieces | 12 of up to 450 frames |
| each piece | 10-17 s, upload + AI + download |
| joined | 5257 frames - exactly the number that went up |
| the pod | one session, 4 min 15 s, terminated after the last piece |

```
POST /v2/<endpoint>/batch           -> {"id": "batch_...", "status": "DRAFT"}
POST /v2/<endpoint>/batch/<id>/requests   -> add more jobs (10 MB a call)
POST /v2/<endpoint>/batch/<id>/finalize    -> set it loose
GET  /v2/<endpoint>/batch/<id>             -> progress
GET  /v2/<endpoint>/batch/<id>/requests    -> the answers, paginated
```

`worker/serverless.py` is that worker. It runs the same `process()` the pod
runs, so the two cannot drift apart, and it takes the job in either of two
shapes:

- `{"video": "<base64>"}` - the clip itself, up to the ten megabytes the
  API takes in one call.
- `{"video_url": "https://..."}` - where the worker should fetch it. This is
  the one for anything bigger.

The answer comes back the same way: the video as base64, or - when the job
carries storage details (`s3Config`) - the key it was written to in the
bucket, which is how a hundred-megabyte result gets home.

`tools/batch_run.py` drives the whole thing: it reads a list of clips, puts
them in a batch, finalizes it, waits, and writes the answers out.

```bash
python3 tools/batch_run.py --endpoint <id> --outdir out/ clip*.mp4
```

### The two kinds of serverless endpoint

Runpod has two, and they want different things from the container:

| | QUEUE | LOAD_BALANCER |
| --- | --- | --- |
| the container speaks | the runpod SDK, which *pulls* the job | plain HTTP, your own paths |
| you write | a `handler(event)` function | whatever HTTP you like |
| a long job | fine - it is not a request | it is a request: Cloudflare's 100 s still applies |
| a pile of jobs | the batch API | no queue, so no batch |

The queue endpoint pulls the job from `RUNPOD_WEBHOOK_GET_JOB` and posts
the answer back to `RUNPOD_WEBHOOK_POST_OUTPUT`; it never posts anything
to the container. A plain HTTP server therefore sits there forever with a
job in the queue that never arrives, which is exactly what happened first.

The load-balancing endpoint forwards the client's HTTP request to the
container and returns the response, which is our own worker protocol
unchanged - but it also asks the worker `GET /ping` until it answers 200.
Until it does, the worker is never given traffic, and the symptom is the
same: jobs in the queue, workers idle, nothing wrong in the logs.

**So the worker answers `/ping`.** That one route is the difference
between a serverless endpoint that works and one that quietly does
nothing.

Measured on the queue endpoint, with the clip of 300 frames:

| | |
| --- | --- |
| submitted | IN_QUEUE, then IN_PROGRESS |
| done | COMPLETED in 53 s, of which 29.6 s on the card |
| the answer | the video as base64, 11.0 MB, 2544x1920 |

The batch API takes a bare JSON array of `{"input": {...}}` - the docs
show `{"requests": [...]}`, but the API answers "cannot unmarshal object
into Go value of type []*job.JobInput" for that. The batch is created
empty (`POST /batch` with `[]`), filled
(`POST /batch/<id>/requests`), then set loose (`POST /batch/<id>/finalize`);
a batch that is not finalized is never run.

#### The batch API does not use the endpoint's workers

Measured on 30 Oct 2026, on the queue endpoint `a76yvmlq4f5q6j`, with two
batches finalised hours earlier (one request each, both `IN_QUEUE`):

| | |
| --- | --- |
| the endpoint switched on | `workers.max = 1`, a worker came up `ready`/`idle` |
| a normal `/run` job | COMPLETED in 53 s, 14.7 MB answer - the worker is alive and healthy |
| the batch requests, 13 min later | still `IN_QUEUE`, `requestCompleted = 0` |
| the endpoint's live queue | `inQueue: 0` - the batch requests never enter it |

Batch requests do not go through the endpoint's own workers. They sit in a
batch queue that Runpod drains with **dedicated batch workers**, scheduled on
global queue urgency and off-peak capacity - the docs say plainly that start
times are not guaranteed and to plan for multi-hour latency. So `workers.max`
is not the switch; the only switch is Runpod's own scheduler, which is not
ours to push.

**What that means for us:** for a handful of clips, use `/run`. It is
asynchronous too (submit, poll `GET /v2/<endpoint>/status/<id>`), it has no
100 s cap, and it completes in about a minute. The batch API only earns its
keep for a large pile of jobs that can wait hours; leave the two finalised
batches as a canary and see whether Runpod ever picks them up.

Two lessons from the same window: the v2 REST API has **no PATCH on the
endpoint itself** (`PATCH /v2/<id>` answers 404 - the change goes through
GraphQL, which the console uses), so switching workers on and off from a
script is not a plain REST call; and `idleTimeout: 5` did **not** scale the
idle worker down, so a window that is left open keeps billing. Park the
endpoint as soon as the last job is done rather than leaving it warm.

### The two endpoints that are set up

Both are built from `docker.io/bzz1/upscaler:v2`, and both are **parked at
zero workers**, because a warm worker is a rental that bills - about
$0.89 an hour each on a 4090, and Runpod kept three of them warm on the
queue endpoint long after the work was done.

| | id | what it is |
| --- | --- | --- |
| `upscaler` | `a76yvmlq4f5q6j` | QUEUE, the runpod SDK, jobs are pulled. A long job is not a request, so there is no 100-second cap. |
| `upscaler-lb` | `xa70ke24lbx886` | LOAD_BALANCER, plain HTTP to the worker, our own paths. |

To use one, turn its workers back on - and turn them off again when the
work is done:

```bash
# on
curl -X PATCH https://api.runpod.ai/v2/a76yvmlq4f5q6j \
  -H "Authorization: Bearer $RUNPOD_API_KEY" -H "Content-Type: application/json" \
  -d '{"workers": {"min": 0, "max": 3, "idleTimeout": 5}}'
# off
... -d '{"workers": {"min": 0, "max": 0, "idleTimeout": 5}}'
```

The pod route needs none of this: a pod is created for one job and
terminated in a `finally`, so it only ever bills for the minutes it is
actually working.

## Container images

`worker/Dockerfile` builds the fast image. Two things are still on the
user's side of the line:

- The image has to reach the rental provider. Build it, push it to a
  registry, and put the name in the GUI's image field.
- The DLoRAL weights (`model.pkl`, `DAPE.pth`) are a Google Drive download
  and are not in the repository, so the DLoRAL image cannot be built without
  them.
