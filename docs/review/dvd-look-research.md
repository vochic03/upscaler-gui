# The DVD look: what it is, and what to add to a local Mac pipeline

## The baseline recipe is a finishing recipe

The thread you pointed at is 25 posts across 2 pages, not 25 pages; it opened 2026-01-14, last reply 2026-02-03. Its chain: Hybrid QTGMC from selur.de (free) with KNLMeansCL denoising, FFV1 lossless output, PAL pixel aspect 1.09, output 1024×576; two Topaz passes with Improve details and Sharpening both near −30; Proteus as base with the Iris Medium pass over it at 65 % opacity, plus Noise set to 3 in the NLE to "conceal washed-out patches that were recovered from MPEG compression" [1].

The aesthetic lives in the **finishing** stages, not the AI stage. SeedVR2 is there as a fidelity step; the look comes from the two −30/−30 passes, the 65 % Iris overlay and the noise added at the end. The thread's ≥16 GB NVIDIA box is not what makes the picture look the way it does.

Two more points deserve separating from the folklore. The downscale-first consensus (about 448×252 for 16:9, 422×308 for 4:3, before a ×3 SeedVR2 pass) is a faithfulness trick: a diffusion restorer given an already-upscaled input has no real low-frequency prior to work from. And mayday's remark that YADIF can beat QTGMC when interlace artefacts are light is under-discussed: QTGMC is invasive, and invasiveness costs the fine texture that makes a soft image read as "film" rather than "mush".

## Three kinds of soft

1. **Bandwidth-limited** — you did not oversharpen; detail below the source's modulation transfer function is gone.
2. **Blurred** — you added blur, via negative unsharp or a low-pass.
3. **Chroma-only softness** — luma edges stay crisp, colour is coarse and smeared relative to them.

Your "polished" preset implements (2), and its grain and unsharp are luma-global, so the chroma axis is untouched. That is why it reads "soft but clean" rather than "DVD". A DVD-Video disc is 8-bit YCbCr with 4:2:0 chroma subsampling [22] — half the chroma resolution in each direction. Genuinely DVD-like material is **asymmetric**: crisp-ish luma, soft colour. That asymmetry is the cheapest thing you are missing.

The tell is colour fringing. Overshoot gives fringes — red one side, blue the other — and is a defect. Subsampling gives smear, colour detached from the edge it belongs to, and is the desired artefact. Fringe and no smear means you sharpened a soft source instead of restoring it.

## Faded is a range operation

"Faded" is lifted blacks, rolled highlights, slightly reduced saturation — a range and transfer change, not a look-up table. Your ffmpeg 8.0.1 build has `colorlevels` with independent per-channel `rimin/gimin/bimin` and `rimax/gimax/bimax` [19]. Put it as a stage *before* the colour grade rather than inside the LUT, so lifting blacks does not also flatten the AI pass's contrast.

The adjacent decision is Rec. 601 versus Rec. 709. PAL/DVD-era material is 601; a 709 pipeline treating it as 709 gets washed-out or crushed blacks depending on direction. Your build has `colorspace` and `colortemperature`. Make space and range explicit per-preset fields, because "faded" is exactly the difference between a correct 601 interpretation and a wrong one.

## Grain: the piece most likely to be wrong

The `noise` filter has two opposite failure modes and neither is film grain. Reading the source, the per-component random shift table is re-initialised inside a block guarded by `if ((!fp->rand_shift_init || (fp->flags & NOISE_TEMPORAL)) && fp->strength)`, which runs every frame [20]. Without the `t` flag the table is built once and reused for every frame — a frozen static pattern, like dirt on a lens. With `t` a fresh pattern is drawn every frame: temporally uncorrelated digital noise, like a broken sensor. Film grain is neither — it is temporally *correlated*, the same emulsion pattern persisting and drifting slowly.

`perlin` gives the third thing: noise "with local continuity in space… patterns with continuity in space and time", with `tscale` controlling time variation speed [19]. Set `size` to the output frame, `rate` to the output frame rate, and a *large* `tscale` so the pattern evolves over seconds, not frames. `xscale`/`yscale` stretch it; `octaves` and `persistence` shape the spectrum.

Two refinements. `perlin` can be confined to the chroma planes via `extractplanes`/`geq`, and `noise` takes independent per-component strengths (`c0s`, `c1s`, `c2s`) — so low luma grain with higher chroma grain is directly expressible, which is where DVD-era artefacts lived. And grain belongs after the upscale, soften and AI blend, immediately before the encode: grain then a moderate-bitrate encode is the classic film-like signature, while grain then an aggressive denoise or low-CRF x264 pass destroys it. Halation belongs here too: `gblur` a luma-only copy and screen it back with `maskedmerge` at 5–10 %, a soft warm halo around highlights that digital never has.

## The finishing half is already on your Mac

Verified against your own Homebrew ffmpeg 8.0.1 arm64 build, every look-relevant filter is present — `perlin`, `cas`, `chromashift`, `chromanr`, `colorlevels`, `curves`, `colorbalance`, `deband`, `gradfun`, `hqdn3d`, `nlmeans`, `gblur`, `maskedmerge`, `lut3d`/`haldclut`, the `spp`/`fspp`/`pp7` MPEG deblockers and the `bwdif`/`w3fdif`/`fieldmatch` deinterlacers [19]. Absent: `zscale`, `libplacebo`, `frei0r`, `libvmaf`. The missing `frei0r` is the one that would have given you glow/bloom plugins; `gblur` plus `maskedmerge` covers it.

Three consequences. **`cas` is present**, so Contrast Adaptive Sharpening at low strength needs no new dependency, and it beats negative unsharp for "soft but not mushy" because it does not ring on edges — and edge ringing is the classic unsharp giveaway. A substitution, not an addition. **`chromashift` is present**, with `cbh`/`cbv`/`crh`/`crv` in ±255 and `edge=smear|wrap`: one or two pixels of `crh`/`cbh` produces colour displacement relative to luma — the chroma-bleed signature — without touching luma sharpness. The highest-value single addition to your pipeline. And **`hqdn3d` defaults here** are `luma_spatial` 4.0, `chroma_spatial` 3.0, `luma_tmp` 6.0, `chroma_tmp` 4.5 [19]; `deband` and `gradfun` fight banding but only work *before* the upscale, on the source's own flat areas.

One part of the thread's recipe cannot be reproduced in ffmpeg as-is: upstream FFmpeg has no QTGMC filter, and your build offers only `bwdif`, `w3fdif`, `yadif`, `fieldmatch`. QTGMC proper needs AviSynth or VapourSynth, where the settings that matter here are `smode` (1 selects by median for a softer, more linear result; 0 is sharper but makes noise more visible), plus `SourceMatch`, `Lossless`, `Sharpness`, `TR2`, `ThSAD2` [21]. `w3fdif` plus `fieldmatch` is the cheap approximation.

## What the AI pass can and cannot do

SeedVR2's own README states the opposite of what this look needs: "due to the strong generation ability, Our methods tend to overly generate details on inputs with very light degradations… leading to oversharpened results occasionally (especially on small resolutions, e.g., 480p)" [2]. A DVD source upscaled from 480p is exactly that case. The AI arm must be *told to stay out of the way*: lower the diffusion blend weight, raise the lanczos arm's.

That is why your existing "blend with a plain lanczos enlargement" step is the most valuable stage you already have. Promote it from an implementation detail to a named parameter — "restoration strength" — and set it low for the DVD-flavoured preset. It is the single knob trading fidelity for the look, so it belongs at preset level.

## Prompt-guided super-resolution

STAR's abstract names the trade-off: text-to-video priors give realistic spatial detail and temporal consistency, but "compromised fidelity due to the strong generative capacity of powerful T2V models (e.g., CogVideoX-5B)" [7]. Its prompt interface is none, auto-generated via Pllava captioning, or a manual text file [6] — and the prompts are *scene descriptions*, steering what is plausible in the frame, not how the frame is graded.

So "16mm film, soft halation, faded colours" is not a style the model was trained on; in a video prior, "film" means clean cinematic video, not degraded DVD. The mechanism has no channel through which chroma coarseness, black lift or grain structure can be requested. Prompt guidance in VSR is a *correctness* tool, not a style tool. Do not add a prompt field.

Kandinsky 6.0 Video's new video-file input is likewise not the restoration path it appears to be. Its README is precise: "an audio file drives the video, and a video file drives the audio" [4][5]. That mode regenerates the *soundtrack* and keeps the source frames — generation, not picture restoration. The genuine video super-resolution path in that family is `kandinsky-6-sr`, whose CLI takes a video file directly.

## The engine landscape

| Engine | Verified cost | Platform | Verdict for this look |
|---|---|---|---|
| Real-ESRGAN NCNN (`realesr-animevideov3`) | GAN, x2/x3/x4; the ncnn binary's *default* model; no outscale (upstream's "cheap arbitrary resize" is an unchecked TODO) [13]. A fork reports `-s 2`/`-s 3` cropping output to the top-left quarter on Apple Silicon [14] | Native | Keep, demoted to a low blend weight |
| Real-CUGAN, Anime4K | Anime-only. Anime4K: "not optimized for downscaled 720p, 480p or standard definition anime (eg. DVDs)" [16][17] | Native | Skip for live action |
| video2x 6.0 | Free; wraps the same ncnn binaries you already drive (Real-ESRGAN, Real-CUGAN, RIFE) + Anime4K v4 GLSL; needs AVX2 + Vulkan; macOS only as a container image [12] | Container only | Skip — re-packages your own engine |
| Waifu2x-Extension-GUI | Free for personal use; "portable package for Windows x64 PCs with Intel/AMD/Nvidia GPUs" [15] | Windows only | Skip |
| Topaz Video AI | Closed. Video Personal $299/yr ($39/mo annual, $59/mo without); Video Pro $699/yr ($74/mo annual) [18] | macOS, Windows | The Iris/Proteus behaviour you imitate — a reference to compare against, not a pipeline stage |
| SeedVR2 3B/7B | One-step diffusion VR; 1×H100-80G handles 100×720×1280, 4× for 1080p/2K (sp_size=4) [2]. Community: 0.85 fps on RTX 5070Ti, 1.2 fps on RTX 5080 [1] — anecdotal | Rented CUDA | Oversharpens light-degradation small-res input by its own docs; downscale first |
| Kandinsky 6.0 VSR | KVAE → latent upscaler → tiled DiT → KVAE decode; x2/x4/x2.25; 5-second clips; Hanning-blend stitching. 768×512→1536×1024 (9 tiles) in 41 s; →3072×2048 (25 tiles) in 118 s (2-step distilled DiT, 121 frames @24 fps) [3] | Rented CUDA | Best-constructed option, but its 5-second clip structure fights a per-video pipeline |
| DLoRAL | 346 s per 50 frames at 512×512 → 2048×2048 on one A100; STAR scores 2830 s on the same benchmark [8]. Needs mmcv-full + mmedit [9] | Rented CUDA | Too slow; research code |
| STAR | ~39 GB VRAM for ×4 on 72 frames of 426×240; repo recommends ≥24 GB [6]; 2830 s/50 frames on A100 [8] | Rented CUDA, ≥24 GB | Wrong axis — see above |
| FlashVSR / FlashVSR+ | ~17 fps at 768×1408 on one A100, ~12× faster than prior one-step diffusion VSR [10]; FlashVSR+ does untiled 4K on one 24 GB RTX 4090 [11] | Rented CUDA; Block-Sparse Attention verified on A100/A800/H200, RTX 40/50 "currently unknown" [10] | The only diffusion VSR fast enough to iterate on, and the only one plausibly worth renting |

Two conflicts worth carrying. SeedVR2's README states H100-class memory requirements, while "16 GB NVIDIA" in circulation comes from the community thread's downscale-first workflow at 484×272, not the README's 720×1280 [1][2]. And STAR's 39 GB comes from the README's own 426×240 toy example while the same README recommends 24 GB [6] — treat 39 GB as the default-configuration cost, not a floor.

## Additions, in order

1. **Chroma-axis pass.** `chromashift` at 1–2 px plus `chromanr` to smooth. Biggest perceptual win, zero new dependencies.
2. **Perlin grain**, luma and chroma separately, after the AI blend and before the encode.
3. **Halation.** `gblur` a luma-only copy and screen it back with `maskedmerge` at 5–10 % — a soft warm halo around highlights, a film print's light scatter that digital never has.
4. **Explicit range stage.** `colorlevels` black lift and highlight roll, separate from the LUT.
5. **`cas` at low strength**, replacing part of the negative unsharp so edges do not ring.
6. **`deband`/`gradfun` before the upscale**, not after.
7. **Restoration-strength slider on the AI blend**, weighted towards the lanczos arm for the DVD preset.
8. **601/709 and range as explicit per-preset fields.**

Items 1–4 belong in the shipped "matte" and "polished" recipes; 5–8 go behind Advanced, except "restoration strength", which defines the look and belongs at preset level.

## What to leave out

A prompt field or any text-to-video-prior engine. Kandinsky 6.0 Video as a video tool, since its video mode regenerates audio rather than picture. Anime4K and Real-CUGAN for live action, both anime-scoped by their own documentation. Waifu2x-Extension-GUI, which is Windows x64. video2x, which re-packages engines you already drive. Frame interpolation, which is not your bottleneck. And most importantly a better upscaler for the DVD-flavoured preset: it needs *less* reconstruction, not more, and the 2026 models are all optimised for more.

## References

[1] https://community.topazlabs.com/t/how-to-achieve-a-dvd-like-image-quality-look-qtgmc-hybrid-topaz-video-ai/100267
[2] https://github.com/ByteDance-Seed/SeedVR
[3] https://github.com/kandinskylab/kandinsky-6-sr
[4] https://github.com/kandinskylab/kandinsky-6
[5] https://github.com/kandinskylab/kandinsky-6/blob/main/docs/generation_modes.md
[6] https://github.com/NJU-PCALab/STAR
[7] https://arxiv.org/abs/2501.02976
[8] https://arxiv.org/abs/2506.15591
[9] https://github.com/yjsunnn/DLoRAL
[10] https://github.com/OpenImagingLab/FlashVSR
[11] https://github.com/OpenImagingLab/FlashVSR_Plus
[12] https://github.com/k4yt3x/video2x
[13] https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan
[14] https://github.com/yetidevworks/Real-ESRGAN-ncnn-vulkan
[15] https://github.com/AaronFeng753/Waifu2x-Extension-GUI
[16] https://github.com/bloc97/Anime4K
[17] https://github.com/bilibili/ailab/tree/main/Real-CUGAN
[18] https://topazlabs.com/pricing
[19] https://ffmpeg.org/ffmpeg-filters.html
[20] https://github.com/FFmpeg/FFmpeg/blob/master/libavfilter/vf_noise.c
[21] https://github.com/DTL2020/QTGMC
[22] https://en.wikipedia.org/wiki/DVD-Video
