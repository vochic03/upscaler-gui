# Hybrid chains - what Selur's forum actually says

Recovered from <https://forum.selur.net> on 30 Oct 2026 with **no login** -
see "How it was retrieved" at the bottom. Everything quoted below is copied
out of the forum; where I draw a conclusion it says so.

What we were after was the filter list: in what order Selur's Hybrid runs
its filters, what a real generated script looks like for an interlaced DVD
rip, and what the QTGMC knobs are actually set to.

---

## 1. Selur's own processing order

From **forum 2, thread 10** ("*hidden* Hybrid options", 06.05.2017), Selur
lists the order of the filters in the **Avisynth extension**:

```
input source
before deinterlace deblocking (depending on configuration)
deringing
color adjustments
deinterlace and frame adjustments
crop
filtering (order depending on configuration) color modifications
deblocking
vinverse
denoise
sharpening
deringing
dehalo
grain handling
stabilize
anit-aliasing
deband
line darkening
noise generator
resize (depending on configuration)
letterbox
subtitle
add logo
interlace / telecine
```

That is the list the pending item asked for. Note the shape of it: **deinterlace
first, denoise after, grain handling late, resize near the end** - the same
order our pipeline uses for the polish pass.

The current VapourSynth path keeps the same idea and exposes it as
*Filtering -> Vapoursynth -> Misc -> Filter Order/Queue* (forum 2, thread
1753). In "Filter Order" mode every filter is called once, in the order the
user set; in "Filter Queue" mode the user adds the filter settings by hand
and can repeat most filters. Crop may be placed anywhere; Deinterlace,
Rotation, FrameInterpolation, Resize, Letterboxing and AddingSubtitle can
only be added once.

---

## 2. A real Hybrid-generated chain for an interlaced DVD upscale

Verbatim from **forum 4, thread 4218** ("Probleme beim Upscaling von
MakeMKV-Dateien"), a 720x576 25 fps top-field-first MPEG-2 going through
Hybrid's VapourSynth path:

```python
import ChangeFPS
import qtgmc
import validate
# Source: '...\Shaolin Wuzang - Episode 1 - Die Rueckkehr des Daemons.mkv'
# Current color space: YUV420P8, bit depth: 8, resolution: 720x576,
# frame rate: 25fps, scanorder: top field first, matrix: 470bg, format: mpeg-2
clip = core.lsmas.LWLibavSource(source="...", format="YUV420P8", stream_index=0, cache=0, prefer_hw=0)
frame = clip.get_frame(0)
# setting color matrix to 470bg.
clip = core.std.SetFrameProps(clip, _Matrix=vs.MATRIX_BT470_BG)
# setting color transfer (vs.TRANSFER_BT601), if it is not set.
if validate.transferIsInvalid(clip):
 clip = core.std.SetFrameProps(clip=clip, _Transfer=vs.TRANSFER_BT601)
# setting color primaries info (to vs.PRIMARIES_BT470_BG), if it is not set.
if validate.primariesIsInvalid(clip):
 clip = core.std.SetFrameProps(clip=clip, _Primaries=vs.PRIMARIES_BT470_BG)
# setting color range to TV (limited) range.
clip = core.std.SetFrameProps(clip=clip, _ColorRange=vs.RANGE_LIMITED)
# making sure frame rate is set to 25fps
clip = core.std.AssumeFPS(clip=clip, fpsnum=25, fpsden=1)
# making sure the detected scan type is set (detected: top field first)
clip = core.std.SetFrameProps(clip=clip, _FieldBased=vs.FIELD_TOP) # tff
original = clip
# Deinterlacing using QTGMC
clip = qtgmc.QTGMC(Input=clip, Preset="Fast", TFF=True, ShowSettings=True, ShowNoise=True, opencl=True) # new fps: 50
# Making sure content is preceived as frame based
clip = core.std.SetFrameProps(clip=clip, _FieldBased=vs.FIELD_PROGRESSIVE) # progressive
# Deinterlacing using QTGMC
original = qtgmc.QTGMC(Input=original, Preset="Fast", TFF=True, ShowSettings=True, ShowNoise=True, opencl=True) # new fps: 50
# Making sure content is preceived as frame based
original = core.std.SetFrameProps(clip=original, _FieldBased=vs.FIELD_PROGRESSIVE) # progressive
# adjusting frame count by adding duplicate frames
original = ChangeFPS.ChangeFPS(clip=original,target_fps_num=50,target_fps_den=1)
# changing range from limited to full range for vsVSMLRT
clip = core.resize.Bicubic(clip, format=vs.YUV420P8, range_in_s="limited", range_s="full")
# setting color range to PC (full) range.
clip = core.std.SetFrameProps(clip=clip, _ColorRange=vs.RANGE_FULL)
# adjusting color space from YUV420P8 to RGBH for vsVSMLRT
clip = core.resize.Bicubic(clip=clip, format=vs.RGBH, matrix_in_s="470bg", range_in_s="full", range_s="full")
# resizing using VSMLRT, target: 1440x1152
clip = vsmlrt.inference([clip],network_path=".../2x_LD-Anime_Compact_330k_net_g.onnx", backend=Backend.ORT_DML(fp16=True,device_id=0,num_streams=1)) # 1440x1152
# changing range from full to limited range for vsVSMLRT
clip = core.resize.Bicubic(clip, format=vs.RGBH,range_in_s="full", range_s="limited")
# making sure 0-1 limits are respected
clip = core.vszip.Limiter(clip=clip, min=[0,0,0], max=[1,1,1])
original = core.resize.Bicubic(clip=original, width=1440, height=1152)
# adjusting output color from: YUV420P8 to YUV420P10 for NVEncModel
original = core.resize.Bicubic(clip=original, format=vs.YUV420P10)
# adjusting output color from: RGBH to YUV420P10 for NVEncModel
clip = core.resize.Bicubic(clip=clip, format=vs.YUV420P10, matrix_s="470bg", range_in_s="full", range_s="limited", dither_type="error_diffusion") # additional resize to allow target color sampling
original = core.text.Text(clip=original,text="Original",scale=2,alignment=7)
clip = core.text.Text(clip=clip,text="Filtered",scale=2,alignment=7)
stacked = core.std.StackHorizontal([original,clip])
# adjusted resizing to achieve PAR 1:1 for preview
# resizing for preview to square pixel to 3072x1152
stacked = core.resize.Bicubic(clip=stacked, width=3072, height=1152)
# set output frame rate to 50fps (progressive)
stacked = core.std.AssumeFPS(clip=stacked, fpsnum=50, fpsden=1)
# output
stacked.set_output()
```

### What that chain does, step by step

1. **Source + colour tagging.** Matrix 470bg, transfer BT601, primaries
   470bg, limited range - all set only if the source does not already say so.
2. **Frame rate pinned** (25) and **scan type pinned** (top field first).
3. **QTGMC** deinterlaces and doubles the frame rate to 50.
4. Content is re-tagged progressive.
5. **Range limited -> full**, then **YUV420P8 -> RGBH**, because the upscale
   model wants full-range RGB.
6. **The upscale itself**: `vsmlrt.inference` with
   `2x_LD-Anime_Compact_330k_net_g.onnx` - that is **Real-ESRGAN Anime
   Compact**, 2x, through the ONNX Runtime DirectML backend.
7. Back to limited range, then to **YUV420P10 with error-diffusion dither**
   for the encoder.
8. The "Original" / "Filtered" side-by-side is **preview only** - it is not
   part of the encoded result.

### The two things that matter most for us

- **Hybrid upscales with Real-ESRGAN Compact (Anime)**, the same model
  family as our `upscayl-realesrgan-x4plus-anime` and
  `xintao-realesr-animevideo` weights. Our CUDA engine and Hybrid are
  upscaling with the same architecture.
- **The upscale happens in full-range RGB, the deinterlace in YUV**. Our
  pipeline upscales in YUV via ffmpeg; if we ever want to match Hybrid
  frame-for-frame, the range handling is the place we differ.

---

## 3. QTGMC: the parameters as Selur actually sets them

Every invocation found in the corpus (225 threads):

| thread | invocation |
| --- | --- |
| f3 4467 | `QTGMC(Input=clip, Preset="Very Slow", InputType=0, TFF=True, TR2=3, SourceMatch=3, Lossless=2, NoiseProcess=2, GrainRestore=0.7)` |
| f3 4472 | `QTGMC(clip, Preset="Fast", TFF=True)` |
| f3 4509 | `QTGMC(clip, Preset="Fast", TFF=False, tools={...})` |
| f3 4513 | `QTGMC(Input=clip2clip, Preset="slow", TFF=False, ...)` |
| f4 4218 | `QTGMC(Input=clip, Preset="Fast", TFF=True, ShowSettings=True, ShowNoise=True, opencl=True)` |
| f4 4258 | `QTGMC(Input=clip, Preset="Placebo", InputType=0, TFF=False, TR2=3, SourceMatch=2, Lossless=2, EZKeepGrain=1.50, NoisePreset="Slow")` |
| f6 4388 | `QTGMC(clip, Preset="Fast", InputType=1, TR2=1, SourceMatch=0, Lossless=0, opencl=True)` |
| f7 4349 | `QTGMC(clip, Preset="Fast", TFF=True, opencl=True)` |
| f7 4358 | `QTGMC(Input=clip2clip, Preset="fast", TFF=False, FPSDivisor=2)` |
| f7 4385 | `QTGMC(clip, Preset="Very Slow", InputType=3, TR2=2, TFF=False, SourceMatch=0, Lossless=0)` |

What the vocabulary means, and which of it the corpus actually exercises:

| knob | values seen | note |
| --- | --- | --- |
| `Preset` | Fast, slow, Very Slow, Placebo | Fast is what Hybrid writes by default; Very Slow / Placebo appear in hand-tuned scripts |
| `TFF` | True / False | top field first or not |
| `InputType` | 0, 1, 3 | the vsQTGMC input-type scale - I did not verify the mapping from the forum, so treat the numbers as observed, not explained |
| `TR2` | 1, 2, 3 | temporal repair strength |
| `SourceMatch` | 0, 2, 3 | source match level |
| `Lossless` | 0, 2 | |
| `NoiseProcess` | 2 | |
| `GrainRestore` | 0.7 | how much grain is put back |
| `EZKeepGrain` | 1.50 | grain kept through the process |
| `NoisePreset` | "Slow" | |
| `opencl` | True | GPU path inside QTGMC |
| `FPSDivisor` | 2 | for the "clip2clip" telecine case |
| `ShowSettings`, `ShowNoise` | True | diagnostics only |

The two grain knobs are the interesting ones for a "polished DVD" look:
`GrainRestore=0.7` keeps most of the original grain through the deinterlace,
`EZKeepGrain=1.50` keeps even more. Both appear in the hand-tuned scripts
rather than in Hybrid's default - Hybrid's own default is `Preset="Fast"`.

---

## 4. The plugin set Selur ships inside vsQTGMC

The `tools={...}` dictionary in the generated scripts names the exact
implementations Hybrid binds to each QTGMC slot. Three sets appear:

```python
tools={'mv': 'mvutensils', 'expr': 'akarin', 'nnedi3': 'znedi3',
       'eedi3': 'vszip', 'dfttest': 'neo_fft', 'nlmeans': 'knlm',
       'bm3d': 'bm3d', 'rg': 'zsmooth', 'median': 'zsmooth',
       'fft3d': 'neo_fft', 'grain': 'noise'}
```

```python
tools={'mv': 'mvutensils', 'expr': 'akarin', 'nnedi3': 'znedi3',
       'dfttest': 'dfttest', 'nlmeans': 'nlm_ispc', 'bm3d': 'bm3d',
       'rg': 'zsmooth', 'median': 'zsmooth', 'boxblur': 'std',
       'scd': 'std'}
```

```python
tools={'mv': 'mvutensils', 'expr': 'akarin', 'tmedian': 'zsmooth',
       'ttempsmooth': 'zsmooth', 'boxblur': 'std',
       'hysteresis': 'hysteresis'}
```

So a current Hybrid build replaces the ageing plugins with **akarin** (expr),
**znedi3**, **vszip's eedi3**, **neo_fft's dfttest**, **knlm**, **bm3d** and
**zsmooth** (rg / median / tmedian / ttempsmooth). Anyone building a
QTGMC chain today should bind the same set - the old `dfttest` and
`nlm_ispc` are the ones that emit the API3 deprecation warnings people
report on the forum.

---

## 5. Denoise filters named in the forum

| filter | mentions in the corpus |
| --- | --- |
| KNLMeans | 30 |
| BM3D / bm3d | 21 |
| TemporalDegrain / TemporalDegrain2 | 12 |
| DFTTest / dfttest | 33 |
| akarin | 29 |
| vszip | 19 |
| znedi3 | 15 |
| EEDI3m | 3 |
| neo_fft | 3 |

vsNR does not appear anywhere in the corpus - the denoise set people
actually use with Hybrid is the QTGMC-internal one above plus
TemporalDegrain.

TemporalDegrain is the one people pair with QTGMC on noisy interlaced
sources (forum 3, thread 4433 - with QTGMC enabled, TemporalDegrain
suppresses the API3 plugin warnings).

---

## 6. The contract for custom sections

From forum 2, thread 2274 ("About Vapoursynth custom sections"):

- A custom section must import everything it needs itself; Hybrid disables
  VapourSynth autoloading as far as it can.
- Hybrid does **not** validate the code you write - wrong code breaks the
  script.
- Hybrid does **not** support changing the frame count in a custom section.
- To let Hybrid know you changed a property, add a comment line:

```
# width 400
# height 400
# scantype tff
# colorformat YUV448
# framerate 120
```

`width`/`height` are 48-32768, `scantype` is one of
progressive/telecine/bff/tff, `framerate` is 1-999.0000 with a dot as the
decimal separator.

---

## 7. How this maps onto our pipeline

Our engine is ffmpeg + Real-ESRGAN (NCNN locally, CUDA on the rented card)
+ RIFE, with the polish (grain, halation, grade) as a post pass. We do not
run VapourSynth.

**We can copy directly**
- the order: deinterlace -> denoise -> upscale -> grade -> grain;
- the model choice: Hybrid upscales with Real-ESRGAN Compact (Anime), and
  that is the family our weights come from;
- the grain philosophy: keep the source grain (`GrainRestore=0.7`,
  `EZKeepGrain=1.50` in the hand-tuned scripts) instead of adding new grain,
  then add only what is missing in the polish pass.

**We cannot copy without VapourSynth**
- QTGMC itself, TemporalDegrain, and the zsmooth/akarin internals. We
  deinterlace with ffmpeg's `yadif`/`bwdif` instead.
- Hybrid's full-range-RGB upscale path. ffmpeg upscales in YUV.

**If we ever want the real thing**, the recipe is above: install
VapourSynth + vsQTGMC + vsmlrt, bind the akarin/znedi3/vszip/neo_fft/knlm/
bm3d/zsmooth tool set, and feed the same source. The chain in section 2 is a
working example end to end.

---

## How it was retrieved (no login needed)

forum.selur.net is **MyBB**, not phpBB, which means it has three doors that
need no account:

| what | url |
| --- | --- |
| RSS, all forums | `https://forum.selur.net/syndication.php` |
| Atom | `https://forum.selur.net/syndication.php?type=atom1.0` |
| RSS, one forum | `https://forum.selur.net/syndication.php?fid=2` |
| static archive, one forum (every thread on one page) | `https://forum.selur.net/archive/index.php/forum-2.html` |
| static archive, one thread (whole thread, stripped) | `https://forum.selur.net/archive/index.php/thread-4218.html` |
| pretty thread page | `https://forum.selur.net/thread-4218.html` |
| search | `https://forum.selur.net/search.php?keywords=QTGMC` |

The RSS feeds carry the **full first post of each thread** inside
`<description>` / `<content:encoded>`, so the feed alone is enough to read
the substance. The archive is the better tool for long threads: it returns
the whole thread, stripped of styling, in one request.

`tools/selur_fetch.py` does this:

```bash
python3 tools/selur_fetch.py forums            # list the forums
python3 tools/selur_fetch.py threads 2 8       # list the threads of those forums
python3 tools/selur_fetch.py pull 1 2 3 4 5 6 7 8   # download them all
```

The pull writes plain text to `work/selur/forum<N>-thread<ID>.txt` with the
URL and title in the header. The run behind this document: **225 threads**
from 8 forums, 1.5 MB of text, no login, no rate-limit trouble (one request
per second is enough).

Forum -> name:

| id | name | threads |
| --- | --- | --- |
| 5 | Talk, Talk, Talk | 0 (container) |
| 2 | General Hybrid Infos | 13 |
| 6 | A/V Talk | 54 |
| 7 | Small Talk | 50 |
| 1 | Hybrid - Support | 0 (container) |
| 3 | Problems & Questions | 52 |
| 4 | Probleme und Fragen | 51 |
| 8 | Step-by-Step | 5 |

Forum 4 is the German half of forum 3 and carries the same answers - it is
where the upscaling thread (4218) lives, which is why it is quoted above.
