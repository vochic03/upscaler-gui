# DRM Capture Player

A local browser app that **decrypts Widevine/ClearKey segments in the page**, feeds them to
Media Source Extensions as *clear* content, and records the result with
`HTMLMediaElement.captureStream()`.

> **Just want to run it?** Read **[CHEATSHEET.md](CHEATSHEET.md)** — the field values, the
> commands, and the catch-up rules. `python3 tools/preset.py mne-play --load` fills the whole
> app from the terminal.

It is the missing half of [WidevineProxy2](https://github.com/DevLARLEY/WidevineProxy2):
that extension gives you the **content key**, this app gives you a **recording** you can
actually watch.

```
WidevineProxy2  ──►  content key + manifest URL + session headers
                          │
                          ▼
        drm-capture  ──►  in-browser CENC decryption  ──►  clear MSE  ──►  captureStream()  ──►  .webm
```

---

## Why a screen recorder cannot do this

Recording a DRM page with OBS / QuickTime / `getDisplayMedia()` gives you a black video area.
There are two independent reasons, and neither is fixable by an extension:

1. **Hardware DRM.** Widevine L1 decodes into a protected surface (HDCP). The compositor shows
   black to anything that is not the protected path.
2. **The browser forbids element capture of protected media.** Chromium's
   `HTMLMediaElement::captureStream()` throws for an element that ever had EME attached:

   ```cpp
   // third_party/blink/renderer/modules/mediacapturefromelement/html_media_element_capture.cc
   if (HTMLMediaElementEncryptedMedia::mediaKeys(element))
       return exception(SecurityError, "Encrypted media cannot be captured.");
   ```

   The check is *"did this element ever run EME"*, **not** *"is the data encrypted"*. Firefox has
   the equivalent guard (`HTMLMediaElement::ContainsRestrictedContent()`).

So the only way to get pixels out of DRM content is to **decrypt it yourself and render it into a
plain `<video>` element**. A plain element has no `MediaKeys`, the capture guard passes, and the
media is origin-clean (fetched with CORS) so `IsMediaDataCorsSameOrigin()` passes too.

This app does exactly that: fetch → decrypt (WebCrypto) → rewrite the init segment → MSE.

---

## Quick start

```bash
cd drm-capture
python3 proxy.py                 # http://127.0.0.1:8765/
```

Then open <http://127.0.0.1:8765/> in Chrome or Firefox.

The proxy does two things:

* serves the app itself;
* replays your requests server-side (`POST /proxy`), so it can send the **session cookies and
  one-time tokens** that WidevineProxy2 captured, and it adds CORS headers the CDNs do not send.
  That keeps the MSE buffers origin-clean, which Chromium requires for `captureStream()`.

### If the stream turns out not to be DRM-protected

Do not go through WidevineProxy2 at all. In Chrome DevTools → **Network** → find the `.m3u8`
request → right-click → **Copy** → **Copy as cURL**, then paste that command into the same box in
the app and press **Parse command**. The parser understands the cURL form: it picks up the
manifest URL and every header (cookies, referer, user-agent) and leaves the key empty. Load →
Play → Record, and the stream is captured without any DRM tooling.

This is also the fast way to *check* whether a page is DRM-protected at all: if the Network tab
shows a plain `.m3u8` with no `license`/`widevine` request next to it, the video is not
Widevine-protected and a plain screen recording of the tab would work too.

### Handing off from WidevineProxy2

1. In the extension popup, press play on the target page so the license exchange happens.
2. Expand the key entry. You get the **content key**, the **manifest URL** and the captured
   **request headers** (the extension also renders a ready-to-run `N_m3u8DL-RE` command).
3. In the app, paste that command into **"Command from the extension popup"** and press
   **Parse command**. The URL, the key and the headers are filled in for you.

   ```
   N_m3u8DL-RE "https://host/manifest.mpd" -H "Cookie: sid=..." --key 1a2b3c:4d5e6f -M format=mkv
   ```
4. **Load** → **Play** → **Record** → stop → download the `.webm`.

The fields can also be typed by hand:

| field | meaning |
|---|---|
| Manifest URL | the MPD or m3u8 the player used |
| Headers | `Name: Value` per line — the cookies/tokens/UA WidevineProxy2 captured |
| Key | the content key (CEK), 32 hex characters |
| KID | optional, for reference only |
| Scheme override | leave on *auto*; the scheme is read from the init segment |

---

## Live streams and DVR

* The player keeps the buffer topped up: it re-reads the manifest every 8 seconds (with a
  cache-buster, otherwise the CDN serves the same stale manifest) and appends whatever
  continues the timeline.
* If the element stalls between two windows, it is nudged across the gap.
* **To rewind**, use the **Catch-up delay** field: it rewrites the manifest URL's `delay`
  parameter (seconds behind the live edge) before loading, so you can pull the window back.
  For an exact programme, seek in the site's own player and copy the `.mpd` request as cURL -
  that URL already carries the right delay and token for the position you want.
* **Keep the tab in front.** Chrome does not open a MediaSource and does not run the media
  pipeline in a hidden tab, and it throttles occluded windows. A player tab that is not the
  active tab of a visible window will sit there "buffering" forever - the app warns about
  this in the log.
* **The audio graph belongs to the element.** An element can only be connected to a
  `MediaElementAudioSourceNode` once, so the graph is cached on the `<video>` and reused. Without
  that, loading a second stream in the same tab throws "already connected previously to a
  different MediaElementAudioSourceNode" and the recording silently loses its audio track.
* **A muted player records silence.** The recording taps the element's audio through a
  WebAudio node, and Chrome applies the element's mute *before* that tap (measured: RMS
  0.016 unmuted, 0.000 muted). The player therefore unmutes the element when you press
  Record, and shows the captured level ("audio: -37 dB") while recording so a silent
  track is obvious.

## Getting the key without the extension

The extension is one way in; a local Widevine CDM is the other. `tools/get_key.py` does the whole
exchange with [pywidevine](https://pypi.org/project/pywidevine/) and a `.wvd` device, and prints the
content key:

```bash
python3 -m pip install pywidevine
python3 tools/get_key.py x <kid-hex> "<license-url>" devices
```

```
*** SUCCESS with 1668035862.wvd ***
KID 8c71ad65-eac0-46f1-9391-0ba092c1a5e1
KEY b66c7c77f3bcf47cbe29d7ce8c07fff2
```

`.wvd` files are Android devices; the ready-to-use ones live at
<https://forum.videohelp.com/threads/413719-Ready-to-use-CDMs-available-here!>
(`https://files.videohelp.com/u/303646/CDMs.zip`). The device only has to be *accepted* by the
licence server — most public ones are, because those servers validate the device's security
level, not its identity.

Note that the key belongs to a **session**: re-extract it when the stream rotates.

## Driving a real Chrome tab

`tools/cdp*.py` talk to a Chrome started with the DevTools protocol, which is how the manifest and
the licence endpoint were found in the first place:

```bash
open -a "Google Chrome" --args \
  --user-data-dir=/tmp/chrome-cdp-profile \
  --remote-debugging-port=9222 \
  --autoplay-policy=no-user-gesture-required
```

* **Chrome 136+ ignores `--remote-debugging-port` on the default profile**, so use a copy of it:
  `cp -Rc ~/Library/Application\ Support/Google/Chrome /tmp/chrome-cdp-profile` (APFS clone, instant).
* `cdp.py eval <tab-id-or-url> '<js>'` runs JavaScript in a tab; `cdp_drive.py click <tab> <selector>`
  dispatches a *trusted* click; `cdp_capture.py <tab> <seconds> out.json` records the traffic.
* Keep the Chrome window **visible**: Chrome freezes occluded windows, and a frozen tab stops
  fetching segments (the player then stalls).

## What it handles

| | |
|---|---|
| Manifests | DASH (SegmentTemplate + SegmentTimeline, SegmentList), HLS (master playlists, `EXT-X-MAP`, `EXT-X-KEY` AES-128 and SAMPLE-AES), a single fragmented MP4 |
| Encryption | `cenc` (AES-CTR), `cbcs` (AES-CBC + 1:9 pattern), `cens` (AES-CTR + pattern), `cbc1` (AES-CBC) |
| IVs | per-sample IVs (8 or 16 bytes) and the constant IV from `tenc` |
| Layouts | separate video/audio representations **and** multiplexed segments (demuxed per track) |
| Live / VOD | both; VOD calls `endOfStream()` so the duration is finite |
| Output | `MediaRecorder` → VP9 + Opus `.webm`, downloadable from the page |

Playback details that are easy to get wrong and are handled here:

* the init segment is rewritten from `encv`/`enca` to the original format (`avc1`, `mp4a`, …) with
  the `sinf` box removed, otherwise MSE treats the track as encrypted;
* the `senc`/`saiz`/`saio` boxes are replaced by `free` boxes **of the same length**, so the moof
  keeps its size and the `trun` data offsets and any `sidx` stay valid;
* MSE wants one SourceBuffer per track, so multiplexed segments are demuxed and each track gets
  its own init segment;
* Chromium caps the number of SourceBuffers at the number of tracks in the *first* init segment,
  so all SourceBuffers are created before any init is appended;
* the codec string is derived from `avcC`/`hvcC`/`esds`, because MSE rejects `video/mp4` without
  a `codecs` parameter.

---

## Files

```
drm-capture/
├── index.html            the app
├── js/
│   ├── app.js            UI layer (parses the WidevineProxy2 command, buttons)
│   ├── pipeline.js       manifest → init → decrypt → MSE (the actual work)
│   ├── boxes.js          ISO BMFF reader/writer: init rewrite, demux, codec strings
│   ├── cenc.js           CENC decryption (cenc / cbcs / cens / cbc1)
│   ├── aes.js            compact AES + CBC/CTR modes (fallback, see below)
│   └── manifest.js       DASH MPD and HLS m3u8 parsing
├── proxy.py              static server + header-replaying fetch proxy
├── presets/              saved stream configurations (manifest, headers, key)
├── test/
│   ├── make_fixture.py   builds the encrypted test fixtures + ffmpeg ground truth
│   ├── selftest.html     runs the pipeline in the browser and dumps the output
│   ├── verify.py         compares the browser output with the ground truth
│   ├── recordtest.html   plays + records, dumps the .webm
│   └── cryptoprobe.html  WebCrypto capability probe (see "browser quirk" below)
├── tools/
│   ├── packager          shaka-packager v3.9.3 binary used to build the fixtures
│   ├── get_key.py        content key extraction with pywidevine (no extension needed)
│   ├── cdp.py            minimal Chrome DevTools Protocol client
│   ├── cdp_drive.py      trusted clicks / typing / blob download over CDP
│   ├── cdp_capture.py    record a tab's network traffic (manifest, licence calls)
│   └── cdp_console.py    collect console messages + Chrome's own error reports
└── devices/              ready-to-use .wvd Widevine devices for get_key.py
```

---

## Validation

The decryption is not "looks right" — it is checked **byte for byte against ffmpeg's own
decryption** of the same files.

```bash
python3 test/make_fixture.py          # needs ffmpeg + tools/packager
python3 test/run_selftest.py          # clears old dumps, drives the browser, compares with ffmpeg
```

`run_selftest.py` needs Chrome on `--remote-debugging-port=9222` and the proxy
serving the app. It does the whole loop for you; the individual steps are:

```bash
# open the selftest pages (one per fixture) in the browser:
#   http://127.0.0.1:8765/test/selftest.html?manifest=...&key=000102030405060708090a0b0c0d0e0f&tag=dash-ctr
python3 test/verify.py
```

```
PASS  dash-ctr/video: identical (775900 bytes)
PASS  dash-ctr/audio: identical (67072 bytes)
PASS  dash-cbc/video: identical (775900 bytes)
PASS  dash-cbc/audio: identical (67072 bytes)
PASS  hls-ctr/video: identical (775900 bytes)
PASS  hls-ctr/audio: identical (67072 bytes)
PASS  hls-cbc/video: identical (775900 bytes)
PASS  hls-cbc/audio: identical (67072 bytes)

8/8 comparisons passed
```

The fixtures are produced by **shaka-packager** (real `pssh`/`schm`/`sinf`/`tenc`, per-segment
`senc`) and the ground truth is ffmpeg decrypting the same files. The comparison covers both
schemes and both layouts (separate DASH representations and multiplexed HLS segments).

The recording itself was verified too: the resulting `.webm` contains a VP9 video track with real
picture content (frame statistics show the expected pattern, not a black frame) and an Opus audio
track with signal (RMS ≈ −21 dB), and it plays back cleanly in ffmpeg.

### Semantics that were reverse-engineered from ffmpeg

The CENC details below are not guesses — they match `libavformat/mov.c` and were confirmed against
ffmpeg's output:

| case | rule |
|---|---|
| `cenc` counter | 16-byte block, only the **low 64 bits** increment (big-endian), one increment per 16-byte block **including a trailing partial block** |
| 8-byte IV | the counter block is `IV ‖ 0x0000000000000000` (IV in the high half) |
| `cbcs` pattern | AES-CBC over the crypt blocks only; the IV for the next crypt run is the **previous crypt block's ciphertext**; the IV is reset to the sample IV at the start of every subsample range |
| `cbcs` without a pattern | plain CBC over `floor(size/16)` blocks; the trailing partial block is left untouched |
| constant IV | used as-is, **not** incremented per sample |

---

## Browser quirk worth knowing

The Electron build used for testing refuses `crypto.subtle.decrypt` with **AES-CBC**
(`OperationError`) while AES-CBC *encrypt*, AES-CTR and AES-GCM *encrypt* all work. Chrome and
Firefox do not have this problem, but `js/aes.js` ships a compact pure-JS AES implementation
(FIPS-197 validated) and the app **feature-tests WebCrypto at runtime** and falls back to it, so
`cbcs` works everywhere.

`test/cryptoprobe.html` shows what your browser supports.

---

## Limits

* **Progressive (non-fragmented) MP4** is not supported — only fragmented MP4 (which is what
  streaming services use).
* **A segment file may hold several fragments.** Some CDNs pack four `moof`/`mdat` pairs into one
  `.m4s`; every `moof` in the file has to be decrypted, not just the first one.
* **Live windows.** A DASH manifest for a live stream advertises only the last window
  (24 s here) and its timeline restarts at 0 for each window. The player therefore re-reads
  the manifest with a cache-buster and appends the segments that continue the timeline. The
  fragments carry absolute timestamps, so they are appended unchanged - re-stamping them
  (or leaving the stale `sidx` in place) is what makes Chromium reject the segment.
* **Segment numbering is not contiguous.** Some segment numbers belong to the other track or
  are not published yet, so the player tolerates holes in the numbering and jumps across
  gaps that appear between two windows.
* **Rewinding on a catch-up (DVR) endpoint.** The catalogue
  (`client_api.php/channel/list/...`) exposes a per-channel `catchup` entry with a duration
  (7 days on MNE Play) and a base asset URL; the player adds `delay=<seconds behind the live
  edge>` to it. The **Catch-up delay** field in the app does exactly that, so you can pull the
  window back by a chosen number of seconds. For an exact programme, the reliable route is to
  seek in the site's own player and copy the `.mpd` request as cURL - that URL carries the
  right delay and token for that position, and the app's cURL parser takes it as-is.
* **The content key is per session.** When a stream rotates keys, re-extract it.
* **Live streams with key rotation** need a fresh key mid-stream; the app does not re-extract
  keys, so use it for a fixed key or for VOD.
* The app is a **local development tool**. `proxy.py` binds to `127.0.0.1` and replays whatever
  headers you give it — do not expose it.
* Content protected by hardware DRM that the browser decrypts *outside* the page (e.g. a native
  decoder) is not reachable this way.

---

## Legal

This decrypts DRM-protected content. That is circumvention of a technological protection measure
and is governed by anti-circumvention law (DMCA §1201 in the US and equivalents elsewhere) as well
as the service's terms of use. Widevine also requires forensic watermarking, which stays in any
recording made through the real CDM.

Use it only for content you own, have licensed, or are otherwise authorised to record — your own
lectures, your company's training material, DRM regression testing of a player you own. The
WidevineProxy2 project carries the same restriction.
