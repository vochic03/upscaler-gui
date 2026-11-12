# The gamma: 1.08

The review left one thing to the user: gamma 1.04 or 1.08 on the Enhance
look. **The user chose 1.08**, and it is now the default in
`engine.LOOKS["enhance"]`.

## What it does to the film

Measured on the finished *Zvezde Granda* film, 60 frames sampled across it
(luma in display levels):

| | p1 | p5 | p25 | p50 | p75 | p95 | p99 | pure black | chroma in the shadows |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| gamma 1.04 | 0 | 0 | 10 | 44 | 105 | 191 | 243 | 18.0% | 72.8% |
| **gamma 1.08** | 0 | 2 | 18 | **56** | 117 | 199 | 248 | **9.5%** | 77.4% |

- the median moves **+11.7 levels**, and the highlights come up with it
  (p95 +8.3, p99 +5);
- the pure black nearly **halves** (18.0% -> 9.5%): the shadows open;
- the shadows pick up **4.6% more colour** - the "the black backdrop goes
  maroon" risk. The shadows were already 73% saturated, so it is a
  continuation of what was there, not a new colour.

The pictures: `images/gamma-104-vs-108-1to1.png` and
`images/gamma-104-vs-108-full.png` - the left is 1.04, the right 1.08.
The two films are both in the review media:
`Zvezde-Granda-upscaled.mp4` (1.08, the film) and
`Zvezde-Granda-upscaled-gamma104.mp4` (1.04, for comparison).

The *polished* and *matte* looks stay at 1.04: they are the film-like
looks and they are deliberately darker, and the choice that was made was
about the Enhance look - the one the film and the default preset use.
