// manifest.js — turn a DASH MPD or an HLS playlist into a concrete
// { init, segments } list that the player can fetch, decrypt and feed to MSE.
//
// Supports:
//   DASH  : SegmentTemplate (incl. SegmentTimeline), SegmentList, SegmentBase
//   HLS   : master playlists, EXT-X-MAP, EXT-X-KEY (AES-128 / SAMPLE-AES)
//   FILE  : a single fragmented MP4 (split locally into init + segments)

import { parseBoxes, parseSidx } from './boxes.js';

export function expandTemplate(tpl, ctx) {
  return String(tpl).replace(/\$(\$|[A-Za-z]+)(%0\d+d)?\$/g, (m, name, fmt) => {
    switch (name) {
      case '$': return '$';
      case 'RepresentationID': return ctx.representationId ?? '';
      case 'Number': return fmtNum(ctx.number, fmt);
      case 'Time': return fmtNum(ctx.time, fmt);
      case 'Bandwidth': return fmtNum(ctx.bandwidth, fmt);
      default: return m;
    }
  });
}

function fmtNum(v, fmt) {
  const n = Number(v);
  if (!fmt) return String(n);
  const width = parseInt(fmt.replace('%0', '').replace('d', ''), 10);
  if (!Number.isFinite(width)) return String(n);
  return String(n).padStart(width, '0');
}

// Build the URL of a segment number from a DASH media template.
export function expandMediaUrl(template, base, rid, number, bandwidth = 0) {
  return abs(base, expandTemplate(template, {
    representationId: rid, number, time: number, bandwidth,
  }));
}

function abs(base, url) {
  try { return new URL(url, base).href; } catch { return url; }
}

// --- DASH -----------------------------------------------------------------

function pickChild(el, name) {
  for (const c of el.children) if (c.tagName === name) return c;
  return null;
}

function pickChildren(el, name) {
  return Array.from(el.children).filter((c) => c.tagName === name);
}

function attr(el, name) {
  return el ? el.getAttribute(name) : null;
}

function num(el, name, dflt) {
  const v = attr(el, name);
  return v === null || v === '' ? dflt : Number(v);
}

// Build the segment list for a DASH representation.
function dashSegments(mpdEl, periodEl, setEl, repEl, baseUrl) {
  const st = pickChild(repEl, 'SegmentTemplate') || pickChild(setEl, 'SegmentTemplate');
  const sl = pickChild(repEl, 'SegmentList') || pickChild(setEl, 'SegmentList');
  const sb = pickChild(repEl, 'SegmentBase') || pickChild(setEl, 'SegmentBase');
  const ctx = {
    representationId: attr(repEl, 'id') || '',
    bandwidth: num(repEl, 'bandwidth', 0),
  };

  if (st) {
    const timescale = num(st, 'timescale', 1);
    const startNumber = num(st, 'startNumber', 1);
    const duration = num(st, 'duration', 0);
    const mediaTpl = attr(st, 'media');
    const initTpl = attr(st, 'initialization');
    const timeline = pickChild(st, 'SegmentTimeline');
    const init = initTpl ? abs(baseUrl, expandTemplate(initTpl, { ...ctx, number: startNumber, time: 0 })) : null;

    const segments = [];
    if (timeline) {
      let t = num(timeline, 't', 0);
      let number = startNumber;
      for (const s of pickChildren(timeline, 'S')) {
        const d = num(s, 'd', 0);
        const rAttr = attr(s, 'r');
        const repeat = rAttr === null ? 0 : Number(rAttr);
        let count = repeat < 0 ? Infinity : repeat + 1;
        if (repeat < 0) {
          const periodDur = num(periodEl, 'duration', 0);
          const totalDur = num(mpdEl, 'mediaPresentationDuration', 0);
          const span = periodDur || totalDur;
          const scale = timescale / (num(mpdEl, 'timescale', 1) || 1);
          count = span > 0 ? Math.ceil((span * scale - t) / (d || 1)) : 1000;
        }
        for (let i = 0; i < count; i++) {
          segments.push({
            url: abs(baseUrl, expandTemplate(mediaTpl, { ...ctx, number, time: t })),
            start: t,
            duration: d,
          });
          t += d;
          number++;
        }
      }
    } else if (duration > 0 && mediaTpl) {
      const total = num(periodEl, 'duration', 0) || num(mpdEl, 'mediaPresentationDuration', 0);
      const count = total > 0 ? Math.ceil((total * timescale) / duration) : 0;
      for (let i = 0; i < count; i++) {
        const t = i * duration;
        segments.push({
          url: abs(baseUrl, expandTemplate(mediaTpl, { ...ctx, number: startNumber + i, time: t })),
          start: t,
          duration,
        });
      }
    }
    const windowBase = segments.length ? segments[0].start : 0;
    return {
      init, segments, type: 'dash-template', timescale, windowBase,
      // The template lets the player ask for the segments *after* the ones
      // the manifest lists: the MPD only advertises the last window, but the
      // URLs are templated and keep going.
      mediaTemplate: mediaTpl, base: baseUrl, rid: ctx.representationId, startNumber,
    };
  }

  if (sl) {
    const timescale = num(sl, 'timescale', 1);
    const startNumber = num(sl, 'startNumber', 1);
    const initEl = pickChild(sl, 'Initialization');
    const init = initEl ? abs(baseUrl, attr(initEl, 'sourceURL')) : null;
    const segments = [];
    const urls = pickChildren(sl, 'SegmentURL');
    const timeline = pickChild(sl, 'SegmentTimeline') || pickChild(setEl, 'SegmentTimeline') || pickChild(repEl, 'SegmentTimeline');
    let t = timeline ? num(timeline, 't', 0) : 0;
    let number = startNumber;
    for (let i = 0; i < urls.length; i++) {
      const media = attr(urls[i], 'media');
      segments.push({ url: abs(baseUrl, media), start: t, duration: 0 });
      number++;
    }
    if (timeline) {
      const ds = pickChildren(timeline, 'S');
      for (let i = 0; i < ds.length && i < segments.length; i++) {
        segments[i].duration = num(ds[i], 'd', 0);
        segments[i].start = t;
        t += segments[i].duration;
      }
    }
    const windowBase = segments.length ? segments[0].start : 0;
    return { init, segments, type: 'dash-list', timescale, windowBase };
  }

  if (sb) {
    // Single-file representation: the whole file is fetched and sliced
    // locally using its sidx.
    const repUrl = abs(baseUrl, attr(repEl, 'id') && repEl.getAttribute('href') || '');
    return { init: null, segments: [], type: 'dash-base', fileUrl: repUrl };
  }
  return { init: null, segments: [], type: 'dash-unknown' };
}

export function parseDash(xmlText, baseUrl) {
  const doc = new DOMParser().parseFromString(xmlText, 'application/xml');
  const err = doc.querySelector('parsererror');
  if (err) throw new Error('MPD is not valid XML: ' + err.textContent.slice(0, 200));
  const mpd = doc.documentElement;
  if (!mpd || mpd.tagName !== 'MPD') throw new Error('not an MPD');
  const period = pickChild(mpd, 'Period');
  if (!period) throw new Error('MPD has no Period');

  const sets = pickChildren(period, 'AdaptationSet');
  // Prefer a video adaptation set; fall back to audio.
  const ordered = [...sets].sort((a, b) => {
    const rank = (el) => {
      const mime = attr(el, 'mimeType') || '';
      const content = attr(el, 'contentType') || '';
      if (mime.startsWith('video') || content === 'video') return 0;
      if (mime.startsWith('audio') || content === 'audio') return 1;
      return 2;
    };
    return rank(a) - rank(b);
  });

  // One track per media kind (video / audio).
  const byKind = new Map();
  for (const set of ordered) {
    const reps = pickChildren(set, 'Representation');
    if (!reps.length) continue;
    const rep = reps.slice().sort((a, b) => num(b, 'bandwidth', 0) - num(a, 'bandwidth', 0))[0];
    const built = dashSegments(mpd, period, set, rep, baseUrl);
    const mimeType = attr(rep, 'mimeType') || attr(set, 'mimeType') || '';
    const contentType = attr(set, 'contentType') || (mimeType.startsWith('audio') ? 'audio' : 'video');
    const kind = contentType === 'audio' ? 'audio' : 'video';
    if (byKind.has(kind)) continue;
    if (!built.segments.length && built.type !== 'dash-base') continue;
    byKind.set(kind, {
      kind,
      init: built.init,
      segments: built.segments,
      mimeType,
      codecs: attr(rep, 'codecs') || attr(set, 'codecs') || '',
      representationId: attr(rep, 'id'),
      type: built.type,
      timescale: built.timescale || 1,
      windowBase: built.windowBase || 0,
      mediaTemplate: built.mediaTemplate,
      base: built.base,
      rid: built.rid,
    });
  }
  if (!byKind.size) throw new Error('no usable Representation in MPD');
  const type = (attr(mpd, 'type') || 'static').toLowerCase();
  return {
    tracks: [...byKind.values()],
    isLive: type === 'dynamic',
    // publishTime is when the manifest was generated, i.e. the live edge.
    // availabilityStartTime is the media-time zero of the whole stream.
    publishTime: attr(mpd, 'publishTime'),
    availabilityStartTime: attr(mpd, 'availabilityStartTime'),
  };
}

// --- HLS ------------------------------------------------------------------

export function parseM3u8(text) {
  const lines = text.split(/\r?\n/).map((l) => l.trim());
  const out = {
    master: false,
    variants: [],
    segments: [],
    init: null,
    mediaSequence: 0,
    endList: false,
    keys: [],
  };
  let pendingKey = null;
  let seq = 0;
  let pendingDuration = 0;
  let pendingTitle = null;

  for (const line of lines) {
    if (!line) continue;
    if (line.startsWith('#EXT-X-STREAM-INF:')) {
      out.master = true;
      const attrs = parseAttrList(line.slice(line.indexOf(':') + 1));
      out.variants.push({ url: null, bandwidth: Number(attrs.BANDWIDTH || 0), codecs: attrs.CODECS || '' });
      continue;
    }
    if (line.startsWith('#EXT-X-MEDIA-SEQUENCE:')) { out.mediaSequence = Number(line.split(':')[1]); seq = out.mediaSequence; continue; }
    if (line.startsWith('#EXT-X-ENDLIST')) { out.endList = true; continue; }
    if (line.startsWith('#EXT-X-MAP:')) {
      const attrs = parseAttrList(line.slice(line.indexOf(':') + 1));
      if (attrs.URI) out.init = attrs.URI;
      continue;
    }
    if (line.startsWith('#EXT-X-KEY:')) {
      const attrs = parseAttrList(line.slice(line.indexOf(':') + 1));
      if (attrs.METHOD === 'NONE') { pendingKey = null; continue; }
      pendingKey = {
        method: attrs.METHOD,
        uri: attrs.URI || null,
        iv: attrs.IV || null,
        keyFormat: attrs.KEYFORMAT || 'identity',
      };
      out.keys.push(pendingKey);
      continue;
    }
    if (line.startsWith('#EXTINF:')) {
      const dur = parseFloat(line.split(':')[1]);
      pendingDuration = Number.isFinite(dur) ? dur : 0;
      continue;
    }
    if (line.startsWith('#')) continue;
    if (out.master) {
      if (out.variants.length && !out.variants[out.variants.length - 1].url) {
        out.variants[out.variants.length - 1].url = line;
      }
      continue;
    }
    out.segments.push({ url: line, duration: pendingDuration, sequence: seq++, key: pendingKey });
    pendingDuration = 0;
  }
  return out;
}

function parseAttrList(s) {
  const out = {};
  // KEY=VALUE pairs, values may be quoted and contain commas.
  const re = /([A-Za-z0-9_-]+)=("(?:[^"]*)"|[^,]*)/g;
  let m;
  while ((m = re.exec(s))) {
    let v = m[2].trim();
    if (v.startsWith('"') && v.endsWith('"')) v = v.slice(1, -1);
    out[m[1].toUpperCase()] = v;
  }
  return out;
}

// --- single fragmented MP4 ------------------------------------------------

// Split a fragmented MP4 into an init segment (ftyp + moov) and media
// segments (each moof + the mdat that follows it).
export function splitFragmentedMp4(buf) {
  const top = parseBoxes(buf);
  const initParts = [];
  const segments = [];
  let currentMoof = null;

  for (const box of top) {
    if (box.type === 'moof') { currentMoof = box; continue; }
    if (box.type === 'mdat' && currentMoof) {
      segments.push(buf.slice(currentMoof.start, box.end));
      currentMoof = null;
      continue;
    }
    if (!currentMoof) initParts.push(buf.slice(box.start, box.end));
  }
  return { init: initParts.length ? concatBytes(initParts) : null, segments };
}

function concatBytes(chunks) {
  let n = 0; for (const c of chunks) n += c.length;
  const out = new Uint8Array(n);
  let p = 0; for (const c of chunks) { out.set(c, p); p += c.length; }
  return out;
}
