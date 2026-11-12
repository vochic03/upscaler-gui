#!/usr/bin/env python3
"""Three changes to the finishing pass: real grain, halation, and cas.

1. Grain. `noise=alls=N:allf=u` builds its random shift table once and reuses
   it for every frame, so the pattern is frozen - dirt on the lens, not film
   grain. `perlin` generates a noise field with continuity in space AND time,
   so the grain drifts the way emulsion does. It is a source filter, so it
   comes in as a third input and is blended in at the end, after the AI
   blend and the softening, where grain belongs.

   Measured on this machine: the frame-to-frame difference of the pattern is
   5.0 at tscale=1, 1.6 at 0.25 and 0.5 at 0.05. So a SMALL tscale is the
   slow one - the grain then holds for a second or two and drifts.

2. Halation. `gblur` a copy of the luma and screen it back at a few per
   cent: a soft halo around the highlights, the light scatter a film print
   has and digital never has. The copy is luma only, so the colour does not
   bleed with it.

3. cas. Contrast Adaptive Sharpening at low strength, in place of part of
   the negative unsharp. It brings the edges back without the ringing that
   gives negative unsharp away, so the picture is soft rather than mushy.
"""
import sys

ROOT = "/Users/vasilijemicic/upscaler-gui"

LOCAL_OLD = '''            filters = []
            if target:
                # The AI gave us its own size; this is the one the user asked
                # for. A downscale here throws nothing away.
                filters.append(f"scale={target[0]}:{target[1]}:flags=lanczos")
            if soften:
                # unsharp takes: luma_msize_x:luma_msize_y:luma_amount:
                #             chroma_msize_x:chroma_msize_y:chroma_amount
                # A negative amount blurs instead of sharpening, which is
                # what "improve details and sharpening to -30" means.
                filters.append(f"unsharp=5:5:{soften}:5:5:{soften}")
            if grain:
                # Static grain, the same pattern in every frame.
                #
                # Per-frame random noise ("temporal") is what Premiere's Add
                # Noise does, but on moving footage at 50 fps it crawls and
                # shimmers badly - the opposite of the calm film look. Static
                # grain gives the analogue texture without the flicker.
                filters.append(f"noise=alls={grain}:allf=u")
            if contrast or saturation:
                filters.append(f"eq=contrast={1 + contrast}:saturation={1 + saturation}")
            lift = float(look.get("black_lift") or 0)
            if lift:
                # Lift the black point a touch: the shadow detail of an
                # analogue transfer never reaches zero.
                filters.append(f"colorlevels=rimin={lift}:gimin={lift}:bimin={lift}")
            filters.append("format=yuv420p")
            # A comma straight after a label is read as an empty filter, so
            # the filters are joined without one.
            parts.append("[mix]" + ",".join(filters) + "[v]")
            ff += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "1:a?"]'''

LOCAL_NEW = '''            filters = []
            if target:
                # The AI gave us its own size; this is the one the user asked
                # for. A downscale here throws nothing away.
                filters.append(f"scale={target[0]}:{target[1]}:flags=lanczos")
            if soften:
                # unsharp takes: luma_msize_x:luma_msize_y:luma_amount:
                #             chroma_msize_x:chroma_msize_y:chroma_amount
                # A negative amount blurs instead of sharpening, which is
                # what "improve details and sharpening to -30" means.
                filters.append(f"unsharp=5:5:{soften}:5:5:{soften}")
            if cas:
                # cas is contrast adaptive sharpening: it brings an edge
                # back without the ringing that gives negative unsharp
                # away, so the picture is soft rather than mushy. It is a
                # substitute for part of the blur, not an addition.
                filters.append(f"cas=strength={cas}")
            if contrast or saturation:
                filters.append(f"eq=contrast={1 + contrast}:saturation={1 + saturation}")
            lift = float(look.get("black_lift") or 0)
            if lift:
                # Lift the black point a touch: the shadow detail of an
                # analogue transfer never reaches zero.
                filters.append(f"colorlevels=rimin={lift}:gimin={lift}:bimin={lift}")
            filters.append("format=yuv420p")
            # A comma straight after a label is read as an empty filter, so
            # the filters are joined without one.
            node = "[mix]" + ",".join(filters)
            fw, fh = target if target else (ai_w, ai_h)
            if halation:
                # A blurred copy of the luma, screened back at a few per
                # cent: the soft halo around highlights that a film print
                # has and digital never has. The copy is luma only, so the
                # colour stays where it is.
                parts.append(f"{node}split=2[base][hlsrc]")
                parts.append(f"[hlsrc]format=gray,gblur=sigma={halation_sigma},"
                             f"format=yuv420p[hlb]")
                parts.append(f"[base][hlb]blend=c0_expr='A+(B-128)*{halation}':"
                             f"c1_expr='A':c2_expr='A'[v1]")
                node = "[v1]"
            if perlin_grain:
                # Film grain: temporally correlated, so it drifts instead of
                # sitting still. Generated at half the size and scaled up,
                # which is both cheaper and more like real grain.
                parts.append(f"[{noise_in}:v]scale={fw}:{fh}:flags=bilinear,"
                             f"format=yuv420p[bn]")
                parts.append(f"{node}[bn]blend=c0_expr='A+(B-128)*{grain_s}':"
                             f"c1_expr='A':c2_expr='A'[v2]")
                node = "[v2]"
            parts.append(node + "null[v]")
            ff += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "1:a?"]'''

LOCAL_VARS_OLD = '''        grain = int(look["grain"])
        contrast = float(look["contrast"])
        saturation = float(look["saturation"])
        treat = bool(blend or soften or grain or contrast or saturation)'''
LOCAL_VARS_NEW = '''        grain = int(look["grain"])
        contrast = float(look["contrast"])
        saturation = float(look["saturation"])
        cas = float(look.get("cas") or 0)
        halation = float(look.get("halation") or 0)
        # A small tscale is the slow one: the pattern then holds for a
        # second or two and drifts, the way emulsion does.
        perlin_grain = look.get("grain_mode") == "perlin" and grain > 0
        grain_s = grain * 0.05
        noise_in = 2
        treat = bool(blend or soften or grain or contrast or saturation
                     or cas or halation)'''

LOCAL_INPUT_OLD = '''        ff = ["ffmpeg", "-y", "-v", "error",
              "-f", "image2pipe", "-framerate", f"{fps:.6f}", "-vcodec", "png", "-i", "-",
              "-i", norm]'''
LOCAL_INPUT_NEW = '''        ff = ["ffmpeg", "-y", "-v", "error",
              "-f", "image2pipe", "-framerate", f"{fps:.6f}", "-vcodec", "png", "-i", "-",
              "-i", norm]
        if perlin_grain:
            # The grain source. random_mode=seed with a fixed seed keeps it
            # the same texture from run to run.
            gw, gh = max(2, (target[0] if target else ai_w) // 2), \\
                     max(2, (target[1] if target else ai_h) // 2)
            ff += ["-f", "lavfi", "-i",
                   f"perlin=size={gw}x{gh}:rate={fps:.6f}:"
                   f"tscale={look.get('grain_tscale') or 0.08}:octaves=2:"
                   f"persistence=0.5:random_mode=seed:random_seed=1"]'''

CLOUD_OLD = '''    filters = []
    if target:
        # The AI gave us its own size; this is the one that was asked for.
        filters.append(f"scale={target[0]}:{target[1]}:flags=lanczos")
    if soften:
        filters.append(f"unsharp=5:5:{soften}:5:5:{soften}")
    if grain:
        filters.append(f"noise=alls={grain}:allf=u")
    if contrast or saturation:
        filters.append(f"eq=contrast={1 + contrast}:saturation={1 + saturation}")
    lift = float(look.get("black_lift") or 0)
    if lift:
        # Lift the black point a touch: the shadow detail of an analogue
        # transfer never reaches zero.
        filters.append(f"colorlevels=rimin={lift}:gimin={lift}:bimin={lift}")
    filters.append("format=yuv420p")
    shift = int(look.get("chroma_shift") or 0)
    if shift:
        # The chroma axis is what makes a picture read as analogue: the
        # colour sits a sample behind the edges.
        filters.append(f"chromashift=cbh={shift}:crh={shift}:edge=smear")
    if look.get("chroma_soft"):
        filters.append("chromanr=thres=60:sizew=8:sizeh=8:stepw=2:steph=2")
    # A comma straight after a label is read as an empty filter, so the
    # filters are joined without one.
    parts.append("[mix]" + ",".join(filters) + "[v]")'''

CLOUD_NEW = '''    filters = []
    if target:
        # The AI gave us its own size; this is the one that was asked for.
        filters.append(f"scale={target[0]}:{target[1]}:flags=lanczos")
    if soften:
        filters.append(f"unsharp=5:5:{soften}:5:5:{soften}")
    if cas:
        # cas is contrast adaptive sharpening: it brings an edge back
        # without the ringing that gives negative unsharp away.
        filters.append(f"cas=strength={cas}")
    if contrast or saturation:
        filters.append(f"eq=contrast={1 + contrast}:saturation={1 + saturation}")
    lift = float(look.get("black_lift") or 0)
    if lift:
        # Lift the black point a touch: the shadow detail of an analogue
        # transfer never reaches zero.
        filters.append(f"colorlevels=rimin={lift}:gimin={lift}:bimin={lift}")
    filters.append("format=yuv420p")
    shift = int(look.get("chroma_shift") or 0)
    if shift:
        # The chroma axis is what makes a picture read as analogue: the
        # colour sits a sample behind the edges.
        filters.append(f"chromashift=cbh={shift}:crh={shift}:edge=smear")
    if look.get("chroma_soft"):
        filters.append("chromanr=thres=60:sizew=8:sizeh=8:stepw=2:steph=2")
    # A comma straight after a label is read as an empty filter, so the
    # filters are joined without one.
    node = "[mix]" + ",".join(filters)
    fw, fh = target if target else (ai_w, ai_h)
    if halation:
        parts.append(f"{node}split=2[base][hlsrc]")
        parts.append(f"[hlsrc]format=gray,gblur=sigma={halation_sigma},"
                     f"format=yuv420p[hlb]")
        parts.append(f"[base][hlb]blend=c0_expr='A+(B-128)*{halation}':"
                     f"c1_expr='A':c2_expr='A'[v1]")
        node = "[v1]"
    if perlin_grain:
        parts.append(f"[2:v]scale={fw}:{fh}:flags=bilinear,format=yuv420p[bn]")
        parts.append(f"{node}[bn]blend=c0_expr='A+(B-128)*{grain_s}':"
                     f"c1_expr='A':c2_expr='A'[v2]")
        node = "[v2]"
    parts.append(node + "null[v]")'''

CLOUD_VARS_OLD = '''    grain = int(look.get("grain", 0))'''
CLOUD_VARS_NEW = '''    grain = int(look.get("grain", 0))
    cas = float(look.get("cas") or 0)
    halation = float(look.get("halation") or 0)
    perlin_grain = look.get("grain_mode") == "perlin" and grain > 0
    grain_s = grain * 0.05
    halation_sigma = max(4, min(24, (target[1] if target else 1080) // 90))'''

CLOUD_CMD_OLD = '''    cmd = ["ffmpeg", "-y", "-v", "error",
           "-i", cloud_video, "-i", original,'''
CLOUD_CMD_NEW = '''    cmd = ["ffmpeg", "-y", "-v", "error",
           "-i", cloud_video, "-i", original]
    if perlin_grain:
        gw, gh = max(2, (target[0] if target else 1920) // 2), \\
                 max(2, (target[1] if target else 1080) // 2)
        cmd += ["-f", "lavfi", "-i",
                f"perlin=size={gw}x{gh}:rate={fps:.6f}:"
                f"tscale={look.get('grain_tscale') or 0.08}:octaves=2:"
                f"persistence=0.5:random_mode=seed:random_seed=1"]
    cmd += ['''

LOOKS_OLD = '''    "polished": {
        "label": "Polished — soft, film-like, the 'DVD' look",
        "denoise": "hqdn3d=3.5:2.5:6:4",
        "blend": 0.35,
        "soften": -0.4,
        "grain": 5,
        "contrast": 0.03,
        "saturation": 0.05,
        "chroma_soft": True,     # coarser, softer chroma
        "black_lift": 0.012,     # the shadows never reach zero
    },'''
LOOKS_NEW = '''    "polished": {
        "label": "Polished — soft, film-like, the 'DVD' look",
        "denoise": "hqdn3d=3.5:2.5:6:4",
        "blend": 0.35,
        # Less blur than before, because cas now brings the edges back
        # without ringing them.
        "soften": -0.28,
        "cas": 0.14,
        "grain": 5,
        "grain_mode": "perlin",
        "grain_tscale": 0.08,
        "halation": 0.07,
        "contrast": 0.03,
        "saturation": 0.05,
        "chroma_soft": True,
        "black_lift": 0.012,
    },'''

MATTE_OLD = '''        "soften": -0.7,
        "grain": 8,
        "contrast": 0.02,'''
MATTE_NEW = '''        "soften": -0.5,
        "cas": 0.08,
        "grain": 8,
        "grain_mode": "perlin",
        "grain_tscale": 0.08,
        "halation": 0.05,'''


def patch(path, pairs):
    src = open(path, encoding="utf-8").read()
    for old, new, label in pairs:
        if old not in src:
            print("MISS:", path, "-", label)
            return False
        if src.count(old) != 1:
            print("AMBIGUOUS (%d):" % src.count(old), path, "-", label)
            return False
        src = src.replace(old, new)
        print("ok:", path, "-", label)
    open(path, "w", encoding="utf-8").write(src)
    return True


ok = True
ok &= patch(ROOT + "/engine.py", [
    (LOOKS_OLD, LOOKS_NEW, "the polished look"),
    (MATTE_OLD, MATTE_NEW, "the matte look"),
    (LOCAL_VARS_OLD, LOCAL_VARS_NEW, "the local variables"),
    (LOCAL_INPUT_OLD, LOCAL_INPUT_NEW, "the perlin input"),
    (LOCAL_OLD, LOCAL_NEW, "the local finishing chain"),
])
ok &= patch(ROOT + "/cloud.py", [
    (CLOUD_VARS_OLD, CLOUD_VARS_NEW, "the cloud variables"),
    (CLOUD_CMD_OLD, CLOUD_CMD_NEW, "the perlin input"),
    (CLOUD_OLD, CLOUD_NEW, "the cloud finishing chain"),
])
print("done" if ok else "SOME PATCHES MISSED")
sys.exit(0 if ok else 1)
