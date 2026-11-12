# The SeedVR2 guide A/B: measured on a card

The brief asked for this and the cloud sandbox could not do it - its network
policy refuses `api.runpod.io`. It was run locally instead, on the same
cap the server enforces.

## What was run

A 5 s clip of a 1080p source (`Emina Jahovic - Dal ona zna (SPOT 2005)`,
1728x1080, 25 fps, 125 frames), capped at **1080x1080** so the target is
1080x674 and the guide has to be chosen. The same clip, the same card, the
same look, two guides:

| | the guide | the upload | the card time |
|---|---|---|---|
| the old code | **538x336** (target / the x2 model's native scale) | 3.9 MB | 5 min |
| the new code | **1078x674** (the source, kept) | 6.4 MB | 5 min |

Four times the pixels in the guide, and the guide is much closer to the
source than the old one was.

## The result: the guide does not decide the detail

Measured over all 125 frames of both outputs:

| | mean luma | laplacian (sharpness) | local edge energy |
|---|---:|---:|---:|
| the old guide (538x336) | 33.5 | 6.407 | 4.680 |
| the new guide (1078x674) | 38.6 | 6.348 | 4.719 |

- **edge energy: +0.8%** - indistinguishable.
- **sharpness: -0.9%** - also indistinguishable, and the new one is
  *softer* by the laplacian measure.
- the new one is sharper on 75 of the 125 frames, so it is not a clean win
  either way.
- **the one real difference is brightness: +5.1 levels.** With more of the
  real picture to work from, the model does not darken the result as much.

**So the guide resolution was not the quality lever.** SeedVR2 invents its
detail at the target resolution whatever it is given; a quarter-size guide
does not make a quarter-size picture. The fix is still worth keeping - it
is free, it gives back five levels of brightness, and it stops the code
throwing away detail for no reason - but it is **not** where the
"Topaz-level" picture comes from.

## Where the picture actually comes from

The **finishing**, not the guide. The old grade took the median of this
material from 41 to 35 and turned the hair and the clothes into solid
black; the new grade (gamma 1.04 on the luma only) takes it to 45 and
keeps the separation. That is the difference you can see. This file is
the proof that the guide is not.

## The pictures

- `images/seedvr2-guide-ab-full.png` - the two results, whole frames.
- `images/seedvr2-guide-ab-1to1.png` - the same region at 1:1.
- the two videos are on the `review-media` release:
  `seedvr2-guide-old.mp4` and `seedvr2-guide-new.mp4`.

## What it cost

$0.11 for the pair, booked against the $25 session cap
(`GET /api/spend`).
