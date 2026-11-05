#!/usr/bin/env python3
"""Add the chroma axis and the lifted black point to the finishing pass.

The colour in an analogue transfer sits half a sample behind the edges,
because the chroma was subsampled and the decoder smeared it sideways.
Nothing else in the chain reproduces that: softening the luma and adding
grain only get you half of the look.

So the polished and matte looks now get three more things:

  * a one-sample chroma delay (chromashift), which puts a soft fringe of
    colour behind every edge,
  * chromanr, which takes the chroma noise down and softens the chroma
    with it,
  * a lifted black point (colorlevels), because the shadows of an analogue
    transfer never actually reach zero.

The chroma filters go after format=yuv420p, because the point is to shift
the subsampled chroma plane, not the full-resolution one.
"""

LOOKS_ADD = {
    "polished": ('        "chroma_shift": 1,      # chroma samples of delay\n'
                 '        "chroma_soft": True,     # take the chroma noise down\n'
                 '        "black_lift": 0.012,     # the shadows never reach zero\n'),
    "matte": ('        "chroma_shift": 1,      # chroma samples of delay\n'
              '        "chroma_soft": True,     # take the chroma noise down\n'
              '        "black_lift": 0.016,     # the shadows never reach zero\n'),
}

ENGINE_OLD = '''        treat = bool(blend or soften or grain or contrast or saturation)'''
ENGINE_NEW = '''        treat = bool(blend or soften or grain or contrast or saturation
                     or look.get("chroma_shift") or look.get("chroma_soft")
                     or look.get("black_lift"))'''

ENGINE_CHAIN_OLD = '''            if contrast or saturation:
                filters.append(f"eq=contrast={1 + contrast}:saturation={1 + saturation}")
            filters.append("format=yuv420p")'''
ENGINE_CHAIN_NEW = '''            if contrast or saturation:
                filters.append(f"eq=contrast={1 + contrast}:saturation={1 + saturation}")
            lift = float(look.get("black_lift") or 0)
            if lift:
                # Lift the black point a touch: the shadow detail of an
                # analogue transfer never reaches zero.
                filters.append(f"colorlevels=rimin={lift}:gimin={lift}:bimin={lift}")
            filters.append("format=yuv420p")
            shift = int(look.get("chroma_shift") or 0)
            if shift:
                # The chroma axis is what makes a picture read as analogue:
                # the colour sits a sample behind the edges. The shift is in
                # chroma samples, so one is half a luma pixel in 4:2:0.
                filters.append(f"chromashift=cbh={shift}:crh={shift}:edge=smear")
            if look.get("chroma_soft"):
                filters.append("chromanr=thres=30:sizew=5:sizeh=5")'''

CLOUD_CHAIN_OLD = '''    if contrast or saturation:
        filters.append(f"eq=contrast={1 + contrast}:saturation={1 + saturation}")
    filters.append("format=yuv420p")'''
CLOUD_CHAIN_NEW = '''    if contrast or saturation:
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
        filters.append("chromanr=thres=30:sizew=5:sizeh=5")'''


def patch(path, pairs):
    src = open(path, encoding="utf-8").read()
    for old, new, label in pairs:
        if old not in src:
            print("MISS:", path, "-", label)
            continue
        if src.count(old) != 1:
            print("AMBIGUOUS (%d):" % src.count(old), path, "-", label)
            continue
        src = src.replace(old, new)
        print("ok:", path, "-", label)
    open(path, "w", encoding="utf-8").write(src)


# 1. the looks -----------------------------------------------------------
src = open("/Users/vasilijemicic/upscaler-gui/engine.py", encoding="utf-8").read()
for name, extra in LOOKS_ADD.items():
    marker = '    "%s": {\n' % name
    i = src.index(marker)
    j = src.index("    },", i)
    block = src[i:j]
    if "chroma_shift" in block:
        print("skip:", name, "already has the chroma pass")
        continue
    src = src[:j] + extra + src[j:]
    print("ok: engine.py - the", name, "look gains the chroma pass")
open("/Users/vasilijemicic/upscaler-gui/engine.py", "w", encoding="utf-8").write(src)

# 2. the two finishing chains --------------------------------------------
patch("/Users/vasilijemicic/upscaler-gui/engine.py", [
    (ENGINE_OLD, ENGINE_NEW, "treat includes the chroma pass"),
    (ENGINE_CHAIN_OLD, ENGINE_CHAIN_NEW, "the local finishing chain"),
])
patch("/Users/vasilijemicic/upscaler-gui/cloud.py", [
    (CLOUD_CHAIN_OLD, CLOUD_CHAIN_NEW, "the cloud finishing chain"),
])
print("done")
