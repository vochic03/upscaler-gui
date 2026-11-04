# drm-capture — quick reference

Everything you need to run this yourself, without calling anyone.

The app lives in `~/drm-capture` and is served by `proxy.py` on
`http://127.0.0.1:8765/`.

---

## 1 · Start the two things

```bash
# 1. the local proxy + app server (leave it running)
cd ~/drm-capture && python3 proxy.py --port 8765

# 2. Chrome with remote debugging, on a cloned profile
#    (a normal Chrome profile refuses --remote-debugging-port since v136)
open -a "Google Chrome" --args \
  --user-data-dir=/tmp/chrome-cdp-profile \
  --remote-debugging-port=9222 \
  --autoplay-policy=no-user-gesture-required
```

Then open <http://127.0.0.1:8765/> in that Chrome.

> The app tab has to be the **active tab of a visible window**. Chrome will
> not open a MediaSource in a hidden tab and throttles occluded windows, so a
> background tab just sits there "buffering".

---

## 2 · Fill the fields — the short version

**You usually do not have to.** Everything you type is remembered in the
browser's local storage, so a reload keeps it all.

To fill it from the terminal instead (this is the one-liner):

```bash
cd ~/drm-capture
python3 tools/preset.py mne-play --delay 7200 --load
```

- `--delay 7200` — two hours back
- `--delay 20:15` — a clock time (today, or yesterday if it is still ahead)
- `--delay 2026-10-29 20:15` — an exact date and time
- omit `--delay` for the live edge
- drop `--load` if you want to press Load yourself

---

## 3 · What goes in each field, if you do it by hand

| Field | Value |
|---|---|
| **Command from the extension popup** | the block in `presets/mne-play.txt` (paste it whole, comments included) → press **Parse command** |
| **Manifest URL** | filled in by Parse. You never type it. |
| **Catch-up delay** | `7200` (seconds) or `20:15` (clock time). Leave empty for live. |
| **Request headers** | filled in by Parse. You never type it. |
| **Content key (CEK)** | `b66c7c77f3bcf47cbe29d7ce8c07fff2` |
| **KID** | `8c71ad65eac046f193910ba092c1a5e1` |
| **Scheme override** | leave on `auto (from the init segment)` |
| **keep the stream going** | leave ticked |

The key and KID also live in `presets/mne-play.key`.

### The command block

```
curl 'https://rtcg-catchup.morescreens.com/RTCG_1_002/MPEG-CBCS/manifest.mpd?delay=0&id=RTCG_1_002&video_id=5&token=f887e07bc1ac59940400f5548c07e65fd2078ceb&authority_instance_id=spectar-prd-rtcg&profile_id=33429&application_installation_id=293643&uuid=6ae267aa359dc0-80137206&subscriber_id=33429&application_id=web&detected_delivery_method=mpegdash&playlist_template=nginx' \
  -H 'referer: https://mneplay.me/' \
  -H 'user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36'
```

---

## 4 · The catch-up delay — how it actually works

Measured against the service, not guessed:

- `delay` is **seconds behind the live edge**, 1:1.
- The published window is **24 s wide** and starts ~20 s before the target
  point. The app re-reads the manifest every 8 s, so the window slides
  forward and the stream keeps running — you are watching a delayed live
  stream, not a file.
- The catch-up buffer reaches back **~4 days** (96 h confirmed; the server
  stops publishing somewhere between 96 h and 108 h).
- The line under the delay field tells you the live edge and where your
  delay lands, so you can type a clock time instead of counting seconds.

### Getting a *specific* programme

Two ways:

1. **Type a clock time.** Put `20:15` in the delay field and press Load. The
   readout confirms: `lands on 28/10 20:46:48`.
2. **Copy the site's own request.** Seek in mneplay.me's player to the
   programme, then in DevTools → Network → right click the `.mpd` request →
   **Copy as cURL**, and paste that into the command box. That URL carries
   the right token and delay for exactly that position. Press **Parse
   command**, then Load.

The second one is the reliable one when you need a specific frame.

---

## 5 · Recording

1. Make sure the app tab is the **active tab of a visible window**.
2. **Load** → it starts playing by itself (no need to press Play).
3. **Record** → a download link appears under the buttons when you stop.
4. The line under the buttons shows the captured level, e.g. `audio: -19 dB`.
   If it says `audio: silent`, the recording has no sound.

Keep the player **playing** while recording — a paused player records a
still frame.

The player keeps about two minutes of video buffered. If you pause for
longer than that, the buffer is dropped and the playhead jumps to the live
edge of the delayed stream, because a paused player never drains its buffer
and Chromium starts refusing new segments once it is full.

The recording is a WebM with VP9 video and Opus stereo.

---

## 6 · If the key ever has to be extracted again

```bash
cd ~/drm-capture
python3 tools/get_key.py x 8c71ad65eac046f193910ba092c1a5e1 \
  "https://mneplay.me/drm.php/widevine?token=<token-from-the-manifest-url>" devices
```

The token is the `token=` parameter of the manifest URL. `devices/` holds
the ready-made `.wvd` CDM files (115 of them). The public `remote.json` CDM
that WidevineProxy2 uses is dead — that is why this exists.

---

## 7 · Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Everything says "buffering", nothing moves | the tab is in the background | bring the Chrome window to the front |
| Video plays, no sound | the element is muted, or the audio context never unlocked | unmute the player; the level line under the buttons shows the captured level |
| `nothing was decrypted` in the log | wrong or missing content key | fill the **Content key** field |
| `HTTP 401/403` from the proxy | the session token expired | copy a fresh cURL from the site and Parse it again |
| `no usable Representation in MPD` | the URL is not a manifest | check you pasted the `.mpd` request, not a segment |
| Recording is a still frame | the player was paused | press Play and record again |

---

## 8 · Checks

```bash
cd ~/drm-capture
python3 test/make_fixture.py      # build test fixtures + ffmpeg ground truth
python3 test/run_selftest.py      # drive the browser, compare with ffmpeg
```

8/8 fixtures are byte-identical to ffmpeg's own decryption. `run_selftest.py`
needs Chrome on `--remote-debugging-port=9222` and the proxy running; it
clears the old dumps first, so a leftover from an earlier run cannot make the
comparison fail for the wrong reason.

---

## 9 · Other tools

```bash
./tools/cdp.py list                          # list Chrome tabs
./tools/cdp.py eval <tab-substring> '<js>'  # run JS in a tab
./tools/cdp_drive.py <tab> front            # bring a tab to the front
./tools/cdp_console.py <tab>                # read the console
./tools/preset.py mne-play --load           # fill the app from the terminal
```

---

## Legal

Widevine's terms forbid recording, and decrypting DRM is circumvention.
This tool is for content you own or are licensed to capture. The author
carries no responsibility for what you do with it.
