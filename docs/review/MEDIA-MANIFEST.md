# The media

Everything below was produced during the work. GitHub refuses any file over
100 MB in a repository, and a repository with half a gigabyte of video in it is
 unpleasant to clone, so the media is attached to a **release** on this
repository instead of living in the git history:

```bash
gh release download review-media --repo vochic03/upscaler-gui
```

The before/after **images** are in the repository itself, under
`docs/review/images/` - those are the part a reviewer can actually read.

| file | size | what it is |
|---|---:|---|
| `Zvezde-Granda-upscaled.mp4` | 338.9 MB | the finished film - 211 s, 1440x1080, 25 fps, SeedVR2 + the Enhance finishing, the original audio muxed back |
| `film-preview-80pct.mp4` | 262.4 MB | the first 80% of the film, finished - the part that survived the disk-full |
| `MNE Play capture (1080p, DVD look).mp4` | 128.6 MB | the MNE Play live capture, 1080p, with the DVD look |
| `Emina Jahovic - Dal ona zna (SPOT 2005) - 1080p.mp4` | 119.1 MB | a second source film, 1080p |
| `Emina Jahovic - Dal ona zna (polished, cloud, faces).mp4` | 65.5 MB | the same, with the face pass (now dormant by default) |
| `Emina Jahovic - Dal ona zna (polished, cloud).mp4` | 60.5 MB | the same, upscaled in the cloud |
| `film-preview-raw.mp4` | 57.9 MB | the raw AI output for the first 20 s, no finishing |
| `mne-play-capture-60s.webm` | 31.4 MB | the DRM capture, 60 s |
| `film-preview-enhanced.mp4` | 29.9 MB | the first 20 s with the Enhance finishing |
| `mne-live-final.webm` | 14.8 MB | the live capture, final |
| `upscaler-rented-pod.mp4` | 10.5 MB | the pod path, the same clip as the serverless path |
| `upscaler-rented-serverless.mp4` | 10.5 MB | the serverless path, the same clip |
| `mne-play-capture.webm` | 9.7 MB | the DRM capture, short version |
| `mne-play-with-audio.webm` | 7.7 MB | the DRM capture with the audio track |
| `mne-upscaled-4k.mp4` | 6.7 MB | the MNE capture upscaled |
| `polished-with-grain.mp4` | 5.2 MB | the polished look with the grain |
| `upscaler-runoff-12s.mp4` | 4.9 MB | a 12 s runoff test |
| `spot-1080p-50fps-sample.mp4` | 4.0 MB | a 50 fps sample of the SPOT source |
| `mne-second-load-with-audio.webm` | 2.5 MB | the second load, with audio |
| `topaz-reference-qlimax.mp4` | 1.6 MB | a Topaz reference |
| `seedvr2-enhance-sample.mp4` | 1.6 MB | a SeedVR2 + Enhance sample |
| `mne-user-tab-with-audio.webm` | 1.5 MB | the capture from the user-tab stream |
| `upscaler-gui-src.zip` | 0.3 MB | the source as it stood earlier in the work |
| `drm-capture-src.zip` | 0.1 MB | the drm-capture project, zipped |
| `drm-capture/tools/packager` | 6.5 MB | shaka-packager v3.9.3, the third-party binary the DRM fixtures are built with. It is not in git: it is a public upstream build, and its OpenSSL PEM header strings trip every credential scanner in the repo. |

