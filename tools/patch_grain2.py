#!/usr/bin/env python3
"""Three changes to the finishing pass: real grain, halation, and cas.

1. Grain. `noise=alls=N:allf=u` builds its random shift table once and reuses
   it for every frame, so the pattern is frozen - dirt on the lens, not film
   grain. `perlin` generates a noise field with continuity in space AND time,
   so the grain drifts the way emulsion does. It is a source filter, so it
   arrives as a third input and is blended in at the very end, after the AI
   blend and the softening, where grain belongs.

   Measured on this machine: the frame-to-frame difference of the pattern is
   5.0 at tscale=1, 1.6 at 0.25 and 0.5 at 0.05. So a SMALL tscale is the
   slow one - the grain then holds for a second or two and drifts.

2. Halation. `gblur` a copy of the luma and screen it back at a few per
   cent: a soft halo around the highlights, the light scatter a film print
   has and digital never has. The copy is luma only, so the colour stays
   where it is.

3. cas. Contrast Adaptive Sharpening at low strength, in place of part of
   the negative unsharp: it brings an edge back without the ringing that
   gives negative unsharp away, so the picture is soft rather than mushy.
"""
import sys

ROOT = "/Users/vasilijemicic/upscaler-gui"

EDITS = {
    ROOT + "/engine.py": [
        # the polished look
        ('''        "blend": 0.35,
        "soften": -0.4,
        "grain": 5,
        "contrast": 0.03,''',
         '''        "blend": 0.35,
        # Less blur than before: cas now brings the edges back without
        # ringing them, which is what unsharp cannot do.
        "soften": -0.28,
        "cas": 0.14,
        "grain": 5,
        "grain_mode": "perlin",
        "grain_tscale": 0.08,
        "halation": 0.07,
        "contrast": 0.03,''', "the polished look"),
        # the matte look
        ('''        "soften": -0.7,
        "grain": 8,
        "contrast": 0.02,''',
         '''        "soften": -0.5,
        "cas": 0.08,
        "grain": 8,
        "grain_mode": "perlin",
        "grain_tscale": 0.08,
        "halation": 0.05,
        "contrast": 0.02,''', "the matte look"),
        # the new variables
        ('''        saturation = float(look["saturation"])
        treat = bool(blend or soften or grain or contrast or saturation
                     or look.get("chroma_shift") or look.get("chroma_soft")
                     or look.get("black_lift"))''',
         '''        saturation = float(look["saturation"])
        cas = float(look.get("cas") or 0)
        halation = float(look.get("halation") or 0)
        # A small tscale is the slow one: the pattern then holds for a
        # second or two and drifts, the way emulsion does.
        perlin_grain = look.get("grain_mode") == "perlin" and grain > 0
        grain_s = grain * 0.05
        treat = bool(blend or soften or grain or contrast or saturation
                     or cas or halation
                     or look.get("chroma_shift") or look.get("chroma_soft")
                     or look.get("black_lift"))''', "the local variables"),
        # the perlin source
        ('''        ff = ["ffmpeg", "-y", "-v", "error",
              "-f", "image2pipe", "-framerate", f"{fps:.6f}", "-vcodec", "png", "-i", "-",
              "-i", norm]''',
         '''        ff = ["ffmpeg", "-y", "-v", "error",
              "-f", "image2pipe", "-framerate", f"{fps:.6f}", "-vcodec", "png", "-i", "-",
              "-i", norm]
        if perlin_grain:
            # The grain source. random_mode=seed with a fixed seed keeps the
            # same texture from run to run.
            gw, gh = max(2, (target[0] if target else ai_w) // 2), \\
                     max(2, (target[1] if target else ai_h) // 2)
            ff += ["-f", "lavfi", "-i",
                   f"perlin=size={gw}x{gh}:rate={fps:.6f}:"
                   f"tscale={look.get('grain_tscale') or 0.08}:octaves=2:"
                   f"persistence=0.5:random_mode=seed:random_seed=1"]''', "the perlin input"),
        # cas
        ('''                filters.append(f"unsharp=5:5:{soften}:5:5:{soften}")''',
         '''                filters.append(f"unsharp=5:5:{soften}:5:5:{soften}")
            if cas:
                # cas is contrast adaptive sharpening: it brings an edge back
                # without the ringing that gives negative unsharp away.
                filters.append(f"cas=strength={cas}")''', "cas"),
        # the static grain gives way to the perlin grain
        ('''            if grain:
                # Static grain, the same pattern in every frame.''',
         '''            if grain and not perlin_grain:
                # Static grain, the same pattern in every frame.''', "the static grain"),
        # halation and the perlin grain at the end of the chain
        ('''            # A comma straight after a label is read as an empty filter, so
            # the filters are joined without one.
            parts.append("[mix]" + ",".join(filters) + "[v]")
            ff += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "1:a?"]''',
         '''            # A comma straight after a label is read as an empty filter, so
            # the filters are joined without one.
            node = "[mix]" + ",".join(filters)
            fw, fh = target if target else (ai_w, ai_h)
            if halation:
                # A blurred copy of the luma, screened back at a few per
                # cent: the soft halo around highlights that a film print has
                # and digital never has. The copy is luma only, so the colour
                # stays where it is.
                parts.append(f"{node}split=2[base][hlsrc]")
                parts.append(f"[hlsrc]format=gray,gblur=sigma={halation_sigma},"
                             f"format=yuv420p[hlb]")
                parts.append(f"[base][hlb]blend=c0_expr='A+(B-128)*{halation}':"
                             f"c1_expr='A':c2_expr='A'[v1]")
                node = "[v1]"
            if perlin_grain:
                # Film grain: temporally correlated, so it drifts instead of
                # sitting still. Generated at half the size and scaled up,
                # which is cheaper and more like real grain.
                parts.append(f"[2:v]scale={fw}:{fh}:flags=bilinear,"
                             f"format=yuv420p[bn]")
                parts.append(f"{node}[bn]blend=c0_expr='A+(B-128)*{grain_s}':"
                             f"c1_expr='A':c2_expr='A'[v2]")
                node = "[v2]"
            parts.append(node + "null[v]")
            ff += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "1:a?"]''',
         "halation and the perlin grain"),
    ],
    ROOT + "/cloud.py": [
        ('''    grain = int(look.get("grain", 0))
    contrast = float(look.get("contrast", 0.0))''',
         '''    grain = int(look.get("grain", 0))
    cas = float(look.get("cas") or 0)
    halation = float(look.get("halation") or 0)
    perlin_grain = look.get("grain_mode") == "perlin" and grain > 0
    grain_s = grain * 0.05
    contrast = float(look.get("contrast", 0.0))''', "the cloud variables"),
        ('''    cmd = ["ffmpeg", "-y", "-v", "error",
           "-i", cloud_video, "-i", original,''',
         '''    cmd = ["ffmpeg", "-y", "-v", "error",
           "-i", cloud_video, "-i", original]
    if perlin_grain:
        gw = max(2, (target[0] if target else w) // 2)
        gh = max(2, (target[1] if target else h) // 2)
        cmd += ["-f", "lavfi", "-i",
                f"perlin=size={gw}x{gh}:rate={fps:.6f}:"
                f"tscale={look.get('grain_tscale') or 0.08}:octaves=2:"
                f"persistence=0.5:random_mode=seed:random_seed=1"]
    cmd += [''', "the perlin input"),
        ('''    if soften:
        filters.append(f"unsharp=5:5:{soften}:5:5:{soften}")
    if grain:
        filters.append(f"noise=alls={grain}:allf=u")''',
         '''    if soften:
        filters.append(f"unsharp=5:5:{soften}:5:5:{soften}")
    if cas:
        # cas is contrast adaptive sharpening: it brings an edge back
        # without the ringing that gives negative unsharp away.
        filters.append(f"cas=strength={cas}")
    if grain and not perlin_grain:
        filters.append(f"noise=alls={grain}:allf=u")''', "cas and the static grain"),
        ('''    # A comma straight after a label is read as an empty filter, so the
    # filters are joined without one.
    parts.append("[mix]" + ",".join(filters) + "[v]")''',
         '''    # A comma straight after a label is read as an empty filter, so the
    # filters are joined without one.
    node = "[mix]" + ",".join(filters)
    fw, fh = target if target else (w, h)
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
    parts.append(node + "null[v]")''', "halation and the perlin grain"),
    ],
}

# the halation blur radius scales with the picture
for path in EDITS:
    src = open(path, encoding="utf-8").read()
    old = '    halation_sigma'
    if "halation_sigma" in src and "halation_sigma =" not in src:
        pass
print("edits prepared")

for path, pairs in EDITS.items():
    src = open(path, encoding="utf-8").read()
    changed = False
    for old, new, label in pairs:
        if old not in src:
            print("MISS:", path, "-", label)
            continue
        if src.count(old) != 1:
            print("AMBIGUOUS (%d):" % src.count(old), path, "-", label)
            continue
        src = src.replace(old, new)
        print("ok:", path, "-", label)
        changed = True
    # the halation blur radius, scaled to the picture
    if "halation_sigma =" not in src:
        if path.endswith("engine.py"):
            anchor = '        grain_s = grain * 0.05\n'
            repl = ('        grain_s = grain * 0.05\n'
                    '        halation_sigma = max(4, min(24, (target[1] if target else ai_h) // 90))\n')
        else:
            anchor = '    grain_s = grain * 0.05\n'
            repl = ('    grain_s = grain * 0.05\n'
                    '    halation_sigma = max(4, min(24, (target[1] if target else h) // 90))\n')
        if anchor in src:
            src = src.replace(anchor, repl)
            print("ok:", path, "- the halation radius")
            changed = True
    if changed:
        open(path, "w", encoding="utf-8").write(src)
        print("written:", path)
print("done")
