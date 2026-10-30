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

## Two things about the rental boxes

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

## Long videos: the serverless path

A pod goes through Cloudflare, and Cloudflare gives that request a hundred
seconds. At the measured speed that is about 1,500 frames of 1080p - twelve
seconds of a film. It is enough for a clip and not enough for a film, and
there is no tunnel around it (see above).

So a full-length video goes through a **serverless endpoint** instead. It is
the same worker, with a different door: the job arrives as JSON and the
answer leaves as JSON, so nothing limits how long a job may run.

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
