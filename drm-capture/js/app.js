// app.js — UI layer over pipeline.js.
//
// The heavy lifting (manifest resolution, init-segment surgery, CENC
// decryption, MSE feeding) lives in pipeline.js so that the test harness
// exercises exactly the same code path as the app.

import { Pipeline } from './pipeline.js';

const $ = (id) => document.getElementById(id);
const state = { pipeline: null, liveEdge: Date.now() };

// How far back the catch-up buffer goes (measured: the server stops
// publishing a window somewhere between 96 h and 108 h).
const DVR_LIMIT_S = 96 * 3600;

function log(msg) {
  const line = `${new Date().toLocaleTimeString()}  ${msg}`;
  const el = $('log');
  el.textContent += line + '\n';
  el.scrollTop = el.scrollHeight;
  console.log('[capture]', msg);
}

function parseHeaders(text) {
  const out = {};
  for (const line of String(text || '').split(/\r?\n/)) {
    const t = line.trim();
    if (!t || t.startsWith('#')) continue;
    const i = t.indexOf(':');
    if (i <= 0) continue;
    out[t.slice(0, i).trim()] = t.slice(i + 1).trim();
  }
  return out;
}

// --- WidevineProxy2 command parser ---------------------------------------

export function tokenize(cmd) {
  const out = [];
  let i = 0;
  while (i < cmd.length) {
    const c = cmd[i];
    if (/\s/.test(c)) { i++; continue; }
    if (c === '"' || c === "'") {
      const end = cmd.indexOf(c, i + 1);
      if (end === -1) break;
      out.push(cmd.slice(i + 1, end));
      i = end + 1;
      continue;
    }
    let j = i;
    while (j < cmd.length && !/\s/.test(cmd[j])) j++;
    out.push(cmd.slice(i, j));
    i = j;
  }
  return out;
}

// Parses the command that WidevineProxy2's popup generates, e.g.
//   N_m3u8DL-RE "https://host/manifest.mpd" -H "Cookie: sid=..." --key 1a2b:3c4d -M format=mkv
// Parses a DevTools "Copy as cURL" command, e.g.
//   curl 'https://host/manifest.mpd' -H 'cookie: sid=...' -H 'referer: https://host/'
// Chrome wraps long commands over several lines with a trailing backslash and
// escapes single quotes inside them. This is the quickest way to pick up the
// manifest URL and the session headers of a stream that turns out not to be
// Widevine-protected (no key needed at all).
// Drop comment lines. A pasted command often comes with notes, and those
// notes must not decide which parser runs nor which URL wins.
function stripComments(text) {
  return String(text || '')
    .split(/\r?\n/)
    .filter((l) => !l.trim().startsWith('#'))
    .join('\n')
    .trim();
}

export function parseCurlCommand(cmd) {
  const text = stripComments(cmd).replace(/\\\s*\n\s*/g, ' ').trim();
  const out = { url: null, headers: {}, key: null, kid: null };
  const headerRe = /(?:-H|--header)\s+('([^']*)'|"([^"]*)"|([^\s]+))/g;
  let m;
  while ((m = headerRe.exec(text)) !== null) {
    let v = m[2] ?? m[3] ?? m[4] ?? '';
    v = v.replace(/\\'/g, "'").replace(/\\"/g, '"');
    const i = v.indexOf(':');
    if (i <= 0) continue;
    out.headers[v.slice(0, i).trim()] = v.slice(i + 1).trim();
  }
  const urls = text.match(/https?:\/\/[^\s'"]+/g);
  if (urls && urls.length) out.url = urls[0].replace(/['"]$/, '');
  return out;
}

export function parseWvp2Command(cmd) {
  const tokens = tokenize(String(cmd || ''));
  const out = { url: null, headers: {}, key: null, kid: null };
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    if (t === '-H' || t === '--header') {
      const v = tokens[++i] || '';
      const idx = v.indexOf(':');
      if (idx > 0) out.headers[v.slice(0, idx).trim()] = v.slice(idx + 1).trim();
    } else if (t === '--key') {
      const v = tokens[++i] || '';
      const idx = v.indexOf(':');
      if (idx > 0) { out.kid = v.slice(0, idx).trim(); out.key = v.slice(idx + 1).trim(); }
      else out.key = v.trim();
    } else if (t.startsWith('-')) {
      if (['-M', '--save-name', '-o', '--output', '-K', '-A', '-c', '--tmp-dir'].includes(t)) i++;
    } else if (!out.url) {
      out.url = t;
    }
  }
  return out;
}

function applyCommand(cmd) {
  const text = stripComments(cmd);
  const isCurl = /(^|\s)curl(\s|$)/i.test(text);
  const parsed = isCurl ? parseCurlCommand(text) : parseWvp2Command(text);
  if (parsed.url) $('manifestUrl').value = parsed.url;
  if (parsed.key) $('key').value = parsed.key;
  if (parsed.kid) $('kid').value = parsed.kid;
  const headers = Object.entries(parsed.headers).map(([k, v]) => `${k}: ${v}`).join('\n');
  if (headers) $('headers').value = headers;
  saveFields();
  log(parsed.url
    ? `parsed ${isCurl ? 'curl' : 'WidevineProxy2'} command`
    : 'nothing usable in that command');
  return parsed;
}

// --- dumping (debug) -----------------------------------------------------

async function dumpToServer(name, bytes) {
  const chunk = 0x8000;
  let bin = '';
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  const res = await fetch('/dump', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, data: btoa(bin) }),
  });
  const j = await res.json().catch(() => ({}));
  log(`dumped ${name}: ${j.bytes || 0} bytes`);
}

// --- catch-up delay -------------------------------------------------------

// The catch-up delay may be written as:
//   7200          - seconds behind the live edge
//   20:15         - a clock time today (or yesterday if it is still ahead)
//   2026-10-29 20:15
// It is converted to the plain number of seconds the manifest expects.
//
// The service's window is exactly "delay seconds behind the live edge": the
// published window starts 20 s before that point and is 24 s wide, and it
// slides forward as the manifest is re-read.
export function parseDelay(text, liveEdgeMs) {
  const s = String(text || '').trim();
  if (!s) return null;
  if (/^\d+$/.test(s)) return Number(s);

  const edge = liveEdgeMs || Date.now();
  let target = null;
  let m = s.match(/^(\d{1,2}):(\d{2})(?::(\d{2}))?$/);
  if (m) {
    const d = new Date(edge);
    target = new Date(d.getFullYear(), d.getMonth(), d.getDate(), +m[1], +m[2], +(m[3] || 0));
    // A clock time that has not happened yet today means yesterday.
    if (target.getTime() > edge) target.setDate(target.getDate() - 1);
  } else {
    m = s.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?$/);
    if (m) target = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0));
  }
  if (!target) return NaN;
  return Math.round((edge - target.getTime()) / 1000);
}

function hhmm(ms) {
  const d = new Date(ms);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

// Same, but with the date when the moment is not on the live edge's day.
function stamp(ms, edgeMs) {
  const d = new Date(ms);
  const e = new Date(edgeMs);
  const sameDay = d.getFullYear() === e.getFullYear() && d.getMonth() === e.getMonth()
    && d.getDate() === e.getDate();
  if (sameDay) return hhmm(ms);
  return d.toLocaleDateString([], { day: '2-digit', month: '2-digit' }) + ' ' + hhmm(ms);
}

// Live readout under the delay field: where the live edge is now and where
// the current delay lands. Without it the delay is pure guesswork.
function updateDelayInfo() {
  const el = $('delayInfo');
  if (!el) return;
  const edge = state.liveEdge || Date.now();
  const secs = parseDelay($('delay').value, edge);
  if (secs === null) {
    el.textContent = `live edge ${hhmm(edge)} · leave empty for the live edge`;
    return;
  }
  if (Number.isNaN(secs)) {
    el.textContent = 'not a delay: use seconds (7200) or a clock time (20:15)';
    return;
  }
  if (secs < 0) {
    el.textContent = 'that time is in the future';
    return;
  }
  const over = secs > DVR_LIMIT_S;
  el.textContent = `live edge ${hhmm(edge)} · lands on ${stamp(edge - secs * 1000, edge)}`
    + ` (${secs} s back)` + (over ? ' · beyond the ~4 day catch-up buffer' : '');
}

// --- actions --------------------------------------------------------------

// Start the element playing.
//
// A catch-up window starts at a large media timestamp, so a freshly loaded
// element sits at the end of its buffer. Without an explicit play() it stays
// paused there and the UI looks like it is stuck buffering forever.
async function startPlayback() {
  const v = $('video');
  if (!v.paused) return;
  try {
    await v.play();
    log('playing');
  } catch (e) {
    if (e.name === 'NotAllowedError') {
      // Autoplay policy: start muted (always allowed) and keep going. The
      // audio still reaches the recording through the WebAudio tap, but the
      // element itself has to be unmuted for the tap to carry any signal.
      v.muted = true;
      try {
        await v.play();
        log('playing (muted: the browser blocked autoplay — unmute for sound)');
      } catch (e2) {
        log('play failed: ' + e2.message);
      }
    } else {
      log('play failed: ' + e.message);
    }
  }
}

async function loadAndPlay() {
  // Optional catch-up: rewrite the manifest URL's delay parameter (seconds
  // behind the live edge) *before* the URL is used.
  const raw = $('delay').value.trim();
  let url = $('manifestUrl').value.trim();
  const delay = parseDelay(raw, state.liveEdge);
  if (raw && Number.isNaN(delay)) {
    log('catch-up: could not read that delay — use seconds (7200) or a clock time (20:15)');
    return;
  }
  if (delay !== null) {
    try {
      const u = new URL(url);
      u.searchParams.set('delay', String(delay));
      url = u.href;
      $('manifestUrl').value = url;
      log(`catch-up: ${delay}s behind the live edge → ${stamp(state.liveEdge - delay * 1000, state.liveEdge)}`);
    } catch (e) {
      log('catch-up: could not rewrite the delay parameter');
    }
  }
  if (!url) { log('enter a manifest URL first'); return; }
  const headers = parseHeaders($('headers').value);
  const keyHex = $('key').value.trim().replace(/[^0-9a-fA-F]/g, '');
  const schemeOverride = $('scheme').value.trim();

  try {
    // Stop the previous run (its timers would fight with the new one).
    if (state.pipeline && state.pipeline.stop) state.pipeline.stop();
    const pipe = new Pipeline($('video'), {
      log,
      dump: $('dumpInit').checked || $('dumpSeg').checked
        ? async (name, bytes) => {
          if (name.startsWith('init-') && !$('dumpInit').checked) return;
          if (name.startsWith('seg-') && !$('dumpSeg').checked) return;
          await dumpToServer(name, bytes);
        }
        : null,
    });
    state.pipeline = pipe;
    window.__capture = { pipeline: pipe, player: pipe.player };
    log('fetching manifest...');
    if (document.hidden) {
      log('!! this tab is in the background — Chrome throttles media there, so playback ' +
          'will keep buffering. Bring this Chrome window to the front.');
    }
    pipe.followLive = $('followLive').checked;
    await pipe.load(url, { headers, keyHex, schemeOverride });
    await pipe.feed();
    // The manifest told us where the catch-up position is; the live edge is
    // that plus the delay we asked for.
    if (pipe.liveEdge) state.liveEdge = pipe.liveEdge;
    updateDelayInfo();
    $('play').disabled = false;
    await startPlayback();
    saveFields();
  } catch (e) {
    log('ERROR: ' + e.message);
    console.error(e);
  }
}

async function toggleRecord() {
  const pipe = state.pipeline;
  if (!pipe) { log('load a source first'); return; }
  const player = pipe.player;
  if (player.recording) {
    if (player.audioMeter) { clearInterval(player.audioMeter); player.audioMeter = null; }
    const blob = await player.stopRecording();
    if (blob) {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'capture-' + new Date().toISOString().replace(/[:.]/g, '-') + '.webm';
      a.textContent = 'download ' + a.download;
      $('download').replaceChildren(a);
    }
    $('record').textContent = 'Record';
    $('record').classList.remove('recording');
  } else {
    try {
      const v = $('video');
      // A muted element feeds the WebAudio tap with silence, so the recording
      // would have no sound.
      if (v.muted || v.volume === 0) {
        log('the player was muted - unmuting so the recording carries sound');
        v.muted = false;
        v.volume = 1;
      }
      if (v.paused) log('the player is paused - press Play first, otherwise the recording is a still frame');
      player.startRecording();
      if (player.audioCtx && player.audioCtx.state !== 'running') {
        log('!! the audio context is suspended - click Play once to unlock sound, ' +
            'otherwise the recording is silent');
      }
      // Report the captured audio level so a silent track is obvious.
      player.audioMeter = setInterval(() => {
        const lvl = player.audioLevel();
        if (lvl === null) return;
        const db = lvl > 0.0005 ? (20 * Math.log10(lvl)).toFixed(0) : '-inf';
        $('audioLevel').textContent = db === '-inf' ? 'audio: silent' : `audio: ${db} dB`;
      }, 1000);
      $('record').textContent = 'Stop recording';
      $('record').classList.add('recording');
    } catch (e) {
      log('record failed: ' + e.message);
    }
  }
}

// --- persistence ----------------------------------------------------------
// Keep the last stream around so a page reload does not lose it.
//
// Everything the user can type is stored, including the command box and the
// catch-up delay, so a reload never costs them a re-paste.

const FIELDS = ['command', 'manifestUrl', 'headers', 'key', 'kid', 'delay'];

function saveFields() {
  try {
    const o = {};
    for (const id of FIELDS) o[id] = $(id).value;
    o.scheme = $('scheme').value;
    o.followLive = $('followLive').checked;
    localStorage.setItem('drm-capture.fields', JSON.stringify(o));
  } catch { /* private mode */ }
}

function restoreFields() {
  try {
    const o = JSON.parse(localStorage.getItem('drm-capture.fields') || '{}');
    for (const id of FIELDS) if (o[id]) $(id).value = o[id];
    if (o.scheme) $('scheme').value = o.scheme;
    if (typeof o.followLive === 'boolean') $('followLive').checked = o.followLive;
  } catch { /* ignore */ }
}

// --- wiring ---------------------------------------------------------------

restoreFields();
for (const id of FIELDS) $(id).addEventListener('input', saveFields);
$('scheme').addEventListener('change', saveFields);
$('followLive').addEventListener('change', saveFields);

// The live edge moves, so the readout has to tick.
$('delay').addEventListener('input', updateDelayInfo);
setInterval(updateDelayInfo, 1000);
updateDelayInfo();

$('parse').addEventListener('click', () => applyCommand($('command').value));
$('load').addEventListener('click', () => loadAndPlay());
$('play').addEventListener('click', async () => {
  const v = $('video');
  if (v.paused) {
    await startPlayback();
  } else {
    v.pause();
    log('paused');
  }
});
$('record').addEventListener('click', toggleRecord);

log('ready — paste the WidevineProxy2 command and press "Parse".');
