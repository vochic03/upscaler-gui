// boxes.js — minimal ISO BMFF (MP4) box reader/writer.
//
// Scope: enough to (a) read moov/moof/traf/tfhd/trun/senc/tenc/sidx and
// (b) surgically rewrite an encrypted init segment into a "clear" one that
// MSE will accept, and strip auxiliary encryption boxes out of media
// segments after decryption.

export const u16 = (b, p) => (b[p] << 8 | b[p + 1]) >>> 0;
export const u32 = (b, p) => ((b[p] << 24) | (b[p + 1] << 16) | (b[p + 2] << 8) | b[p + 3]) >>> 0;
export const u64 = (b, p) => u32(b, p) * 0x100000000 + u32(b, p + 4);
export const fourcc = (b, p) => String.fromCharCode(b[p], b[p + 1], b[p + 2], b[p + 3]);

export function hex(buf) {
  return Array.from(buf, (x) => x.toString(16).padStart(2, '0')).join('');
}

export function unhex(s) {
  const clean = String(s || '').replace(/[^0-9a-fA-F]/g, '');
  const out = new Uint8Array(clean.length >> 1);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(clean.substr(i * 2, 2), 16);
  return out;
}

export function concat(chunks) {
  let n = 0;
  for (const c of chunks) n += c.length;
  const out = new Uint8Array(n);
  let p = 0;
  for (const c of chunks) { out.set(c, p); p += c.length; }
  return out;
}

// Boxes whose payload is just a list of child boxes.
const CONTAINERS = new Set([
  'moov', 'trak', 'mdia', 'minf', 'stbl', 'edts', 'dinf', 'mvex', 'moof', 'traf',
  'mfra', 'udta', 'sinf', 'schi', 'meco', 'tref', 'trgr', 'mvex', 'mfhd', 'trik',
]);

export function parseBoxes(b, start = 0, end = b.length) {
  const out = [];
  let p = start;
  while (p + 8 <= end) {
    let size = u32(b, p);
    const type = fourcc(b, p + 4);
    let hdr = 8;
    if (size === 1) { size = u64(b, p + 8); hdr = 16; }
    else if (size === 0) { size = end - p; }
    if (size < hdr || p + size > end) break;
    const uuidExtra = type === 'uuid' ? 16 : 0;
    out.push({ type, start: p, size, hdr, dataStart: p + hdr + uuidExtra, end: p + size });
    p += size;
  }
  return out;
}

export function buildBox(type, payload) {
  const size = 8 + payload.length;
  const hdr = size > 0xffffffff ? 16 : 8;
  const out = new Uint8Array(hdr + payload.length);
  if (hdr === 16) {
    out[0] = 1;
    const hi = Math.floor(size / 0x100000000);
    const lo = size >>> 0;
    out[4] = (hi >>> 24) & 0xff; out[5] = (hi >>> 16) & 0xff;
    out[6] = (hi >>> 8) & 0xff; out[7] = hi & 0xff;
    out[8] = (lo >>> 24) & 0xff; out[9] = (lo >>> 16) & 0xff;
    out[10] = (lo >>> 8) & 0xff; out[11] = lo & 0xff;
  } else {
    out[0] = (size >>> 24) & 0xff; out[1] = (size >>> 16) & 0xff;
    out[2] = (size >>> 8) & 0xff; out[3] = size & 0xff;
  }
  out[hdr - 4] = type.charCodeAt(0);
  out[hdr - 3] = type.charCodeAt(1);
  out[hdr - 2] = type.charCodeAt(2);
  out[hdr - 1] = type.charCodeAt(3);
  out.set(payload, hdr);
  return out;
}

// Child boxes of a container box. stsd is special: 8 bytes of
// version/flags + entry_count precede the sample entries. Encrypted sample
// entries (encv/enca) are special too: their child boxes (sinf/schi/tenc)
// start after the fixed sample-entry header.
export function childBoxes(b, box) {
  if (box.type === 'stsd') return parseBoxes(b, box.dataStart + 8, box.end);
  if (CONTAINERS.has(box.type)) return parseBoxes(b, box.dataStart, box.end);
  if (box.type === 'encv' || box.type === 'encs' || box.type === 'enct') {
    return parseBoxes(b, box.dataStart + sampleEntryPrefix(b, box, 'vide'), box.end);
  }
  if (box.type === 'enca') {
    return parseBoxes(b, box.dataStart + sampleEntryPrefix(b, box, 'soun'), box.end);
  }
  return [];
}

// Depth-first search for every box of a given type.
export function findBoxes(b, type, box = null, out = []) {
  const roots = box ? [box] : parseBoxes(b);
  for (const bx of roots) {
    if (bx.type === type) out.push(bx);
    for (const child of childBoxes(b, bx)) findBoxes(b, type, child, out);
  }
  return out;
}

// Handler type ('vide' / 'soun') of the trak containing `box`.
export function handlerType(b, box) {
  const hdlrs = findBoxes(b, 'hdlr', box);
  for (const h of hdlrs) {
    // hdlr: version/flags(4) pre_defined(4) handler_type(4)
    const t = fourcc(b, h.dataStart + 8);
    if (t === 'vide' || t === 'soun' || t === 'subt' || t === 'text') return t;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Codec strings
//
// MSE requires an explicit codecs parameter ("video/mp4" alone is rejected),
// so the codec has to be derived from the sample description in the init
// segment: avcC / hvcC / esds.
// ---------------------------------------------------------------------------

function hex2(v) { return v.toString(16).padStart(2, '0'); }

// avc1.<profile><compat><level> from the AVCDecoderConfigurationRecord.
function avcCodec(b, box) {
  const d = box.dataStart;
  if (d + 4 > box.end) return null;
  return `avc1.${hex2(b[d + 1])}${hex2(b[d + 2])}${hex2(b[d + 3])}`;
}

// hvc1.<space><profile>.<compat>.<tier><level>.<constraints> from hvcC.
function hevcCodec(b, box) {
  const d = box.dataStart;
  if (d + 13 > box.end) return null;
  const p = b[d + 1];
  const space = (p >> 6) & 0x3;
  const tier = (p >> 5) & 0x1;
  const profileIdc = p & 0x1f;
  const compat = [];
  for (let i = 0; i < 4; i++) compat.unshift(b[d + 2 + i]);
  const compatStr = compat.map(hex2).join('');
  const level = b[d + 12];
  let constraints = '';
  for (let i = 0; i < 6; i++) {
    const v = b[d + 6 + i];
    if (v === 0) continue;
    constraints += '.' + hex2(v);
  }
  const spaceStr = ['', 'A', 'B', 'C'][space];
  return `hvc1.${spaceStr}${profileIdc}.${compatStr}.${tier ? 'H' : 'L'}${level}${constraints}`;
}

// Read MPEG-4 descriptors (variable length sizes).
function readDescriptors(b, start, end) {
  const out = [];
  let p = start;
  while (p + 2 <= end) {
    const tag = b[p];
    let size = 0;
    let q = p + 1;
    for (let i = 0; i < 4 && q < end; i++) {
      const byte = b[q++];
      size = (size << 7) | (byte & 0x7f);
      if (!(byte & 0x80)) break;
    }
    out.push({ tag, size, start: q, end: Math.min(q + size, end) });
    p = q + size;
    if (size === 0) break;
  }
  return out;
}

// "mp4a.40.2" (AAC-LC) and friends from the esds box.
function audioCodec(b, box) {
  const descs = readDescriptors(b, box.dataStart, box.end);
  const es = descs.find((d) => d.tag === 0x03);
  if (!es) return null;
  const inner = readDescriptors(b, es.start + 3, es.end); // skip ES_ID + flags
  const dcd = inner.find((d) => d.tag === 0x04);
  if (!dcd) return null;
  const objectTypeIndication = b[dcd.start];
  const dsi = readDescriptors(b, dcd.start + 13, dcd.end).find((d) => d.tag === 0x05);
  if (!dsi || dsi.end - dsi.start < 1) return null;
  const asc = b[dsi.start];
  let audioObjectType = (asc >> 3) & 0x1f;
  if (audioObjectType === 31) {
    audioObjectType = 32 + (((asc & 0x07) << 3) | (b[dsi.start + 1] >> 5));
  }
  if (objectTypeIndication === 0x40) return `mp4a.40.${audioObjectType}`;
  return null;
}

// Derive { video, audio } codec strings from an init segment.
export function deriveCodecs(initBuf) {
  const out = { video: null, audio: null };
  const moov = parseBoxes(initBuf).find((b) => b.type === 'moov');
  if (!moov) return out;
  for (const trak of childBoxes(initBuf, moov)) {
    if (trak.type !== 'trak') continue;
    const kind = handlerType(initBuf, trak);
    if (kind !== 'vide' && kind !== 'soun') continue;
    const stsd = findBoxes(initBuf, 'stsd', trak)[0];
    if (!stsd) continue;
    for (const entry of childBoxes(initBuf, stsd)) {
      const kids = parseBoxes(initBuf, entry.dataStart + sampleEntryPrefix(initBuf, entry, kind), entry.end);
      for (const k of kids) {
        if (kind === 'vide') {
          if (k.type === 'avcC') { out.video = out.video || avcCodec(initBuf, k); }
          else if (k.type === 'hvcC') { out.video = out.video || hevcCodec(initBuf, k); }
          else if (k.type === 'vpcC') { out.video = out.video || 'vp09.00.10.08'; }
        } else {
          if (k.type === 'esds') { out.audio = out.audio || audioCodec(initBuf, k); }
          else if (k.type === 'dOps') { out.audio = out.audio || 'opus'; }
          else if (k.type === 'dfLa') { out.audio = out.audio || 'flac'; }
        }
      }
      // Non-AAC audio: the sample entry format itself is the codec.
      if (kind === 'soun' && !out.audio) {
        const f = entry.type;
        if (f === 'ac-3' || f === 'ac-4' || f === 'ec-3' || f === 'Opus' || f === 'fLaC') out.audio = f;
        else if (f === 'mp4a') out.audio = 'mp4a.40.2';
      }
      if (kind === 'vide' && !out.video) {
        const f = entry.type;
        if (f === 'av01') out.video = 'av01.0.04M.08';
        else if (f === 'vp09' || f === 'vp08') out.video = 'vp09.00.10.08';
        else if (f === 'avc1' || f === 'avc3') out.video = 'avc1.42E01E';
      }
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Init segment: encrypted -> clear
// ---------------------------------------------------------------------------

const DROP_FROM_INIT = new Set(['pssh', 'senc', 'saiz', 'saio']);
const SAMPLE_ENTRY_PREFIX = {
  vide: 78,   // bytes of payload before child boxes (VisualSampleEntry)
  soun: 28,   // AudioSampleEntry v0
  subt: 28,
};

// Payload bytes of a sample entry before its child boxes.
function sampleEntryPrefix(b, box, kind) {
  if (kind === 'soun' || kind === 'subt') {
    const version = u16(b, box.dataStart);
    if (version === 1) return 44;
    if (version === 2) return 64;
    return 28;
  }
  return SAMPLE_ENTRY_PREFIX.vide;
}

// Rewrite an 'encv'/'enca' entry into its clear original format ('avc1',
// 'mp4a', ...) and drop the 'sinf' box that carried the encryption info.
function transformSampleEntry(b, box, kind, info) {
  const prefix = sampleEntryPrefix(b, box, kind);
  const childrenStart = box.dataStart + prefix;
  const children = parseBoxes(b, childrenStart, box.end);

  let newType = kind === 'soun' ? 'mp4a' : 'avc1';
  const kept = [];
  for (const c of children) {
    if (c.type === 'sinf') {
      // sinf -> frma (original format) / schm (scheme) / schi -> tenc
      for (const sc of parseBoxes(b, c.dataStart, c.end)) {
        if (sc.type === 'frma') {
          const orig = fourcc(b, sc.dataStart);
          if (/^[a-zA-Z0-9 ]{4}$/.test(orig)) newType = orig;
        } else if (sc.type === 'schm') {
          // schm: version/flags(4) scheme_type(4) scheme_version(4)
          const scheme = fourcc(b, sc.dataStart + 4);
          if (scheme && scheme !== 'NONE') info.scheme = scheme;
        }
      }
      continue; // drop sinf entirely
    }
    kept.push(b.slice(c.start, c.end));
  }

  const fixed = b.slice(box.start + 8, childrenStart);
  const payload = concat([fixed, ...kept]);
  return { buf: buildBox(newType, payload), type: newType };
}

function rebuildStsd(b, box, kind, info) {
  const entries = parseBoxes(b, box.dataStart + 8, box.end);
  const out = [];
  for (const e of entries) {
    if (e.type === 'encv' || e.type === 'enca' || e.type === 'encs' || e.type === 'enct') {
      const r = transformSampleEntry(b, e, kind, info);
      out.push(r.buf);
    } else {
      out.push(b.slice(e.start, e.end));
    }
  }
  const head = b.slice(box.start + 8, box.dataStart + 8);
  return buildBox('stsd', concat([head, ...out]));
}

// Generic container rebuild: keep children except dropped types, recursing
// into the ones we care about.
function rebuildContainer(b, box, kind, info, drop) {
  const children = parseBoxes(b, box.dataStart, box.end);
  const out = [];
  for (const c of children) {
    if (drop.has(c.type)) continue;
    if (c.type === 'trak') { out.push(rebuildTrak(b, c, info)); continue; }
    if (c.type === 'mdia') { out.push(rebuildMdia(b, c, info)); continue; }
    if (c.type === 'minf') { out.push(rebuildMinf(b, c, info)); continue; }
    if (c.type === 'stbl') { out.push(rebuildStbl(b, c, info)); continue; }
    if (c.type === 'stsd') { out.push(rebuildStsd(b, c, kind, info)); continue; }
    out.push(b.slice(c.start, c.end));
  }
  return buildBox(box.type, concat(out));
}

function rebuildStbl(b, box, info) {
  const children = parseBoxes(b, box.dataStart, box.end);
  const out = [];
  for (const c of children) {
    if (c.type === 'stsd') {
      const kind = info.kind;
      out.push(rebuildStsd(b, c, kind, info));
    } else {
      out.push(b.slice(c.start, c.end));
    }
  }
  return buildBox('stsd' === 'stsd' ? 'stbl' : 'stbl', concat(out));
}
function rebuildMinf(b, box, info) {
  const children = parseBoxes(b, box.dataStart, box.end);
  const out = [];
  for (const c of children) {
    if (c.type === 'stbl') out.push(rebuildStbl(b, c, info));
    else out.push(b.slice(c.start, c.end));
  }
  return buildBox('minf', concat(out));
}
function rebuildMdia(b, box, info) {
  const children = parseBoxes(b, box.dataStart, box.end);
  const out = [];
  for (const c of children) {
    if (c.type === 'minf') out.push(rebuildMinf(b, c, info));
    else out.push(b.slice(c.start, c.end));
  }
  return buildBox('mdia', concat(out));
}
function rebuildTrak(b, box, info) {
  const children = parseBoxes(b, box.dataStart, box.end);
  const out = [];
  for (const c of children) {
    if (c.type === 'mdia') {
      info.kind = handlerType(b, c) || 'vide';
      out.push(rebuildMdia(b, c, info));
    } else {
      out.push(b.slice(c.start, c.end));
    }
  }
  return buildBox('trak', concat(out));
}

// Convert an encrypted init segment into a clear one.
//
// Returns { init, scheme, kind, tencBox } where `init` is a byte-identical
// copy of the original except that every 'encv'/'enca' sample entry has been
// rewritten to its original format with the 'sinf' box removed.
export function makeClearInit(initBuf) {
  const info = { scheme: null, kind: 'vide' };
  const top = parseBoxes(initBuf);
  const out = [];
  for (const box of top) {
    if (DROP_FROM_INIT.has(box.type)) continue;
    if (box.type === 'moov') {
      out.push(rebuildContainer(initBuf, box, 'vide', info, DROP_FROM_INIT));
    } else {
      out.push(initBuf.slice(box.start, box.end));
    }
  }
  const tencBoxes = findBoxes(initBuf, 'tenc');
  return {
    init: concat(out),
    scheme: info.scheme,
    kind: info.kind,
    tencBox: tencBoxes[0] || null,
  };
}

// ---------------------------------------------------------------------------
// Media segment: sample table
// ---------------------------------------------------------------------------

const TFHD_BASE_DATA_OFFSET = 0x000001;
const TFHD_SAMPLE_DESCRIPTION_INDEX = 0x000002;
const TFHD_DEFAULT_SAMPLE_DURATION = 0x000008;
const TFHD_DEFAULT_SAMPLE_SIZE = 0x000010;
const TFHD_DEFAULT_SAMPLE_FLAGS = 0x000020;
const TFHD_DEFAULT_BASE_IS_MOOF = 0x020000;

const TRUN_DATA_OFFSET = 0x000001;
const TRUN_FIRST_SAMPLE_FLAGS = 0x000004;
const TRUN_SAMPLE_DURATION = 0x000100;
const TRUN_SAMPLE_SIZE = 0x000200;
const TRUN_SAMPLE_FLAGS = 0x000400;
const TRUN_SAMPLE_CTO = 0x000800;

export function parseTfhd(b, box) {
  const flags = u32(b, box.dataStart) & 0xffffff;
  const trackId = u32(b, box.dataStart + 4);
  let p = box.dataStart + 8;
  const tfhd = {
    flags, trackId,
    baseDataOffset: null,
    defaultSampleDescriptionIndex: 1,
    defaultSampleDuration: 0,
    defaultSampleSize: 0,
    defaultSampleFlags: 0,
    defaultBaseIsMoof: true,
  };
  // Field order is fixed by ISO/IEC 14496-12 §8.8.7.
  if (flags & TFHD_BASE_DATA_OFFSET) { tfhd.baseDataOffset = u64(b, p); p += 8; }
  if (flags & TFHD_SAMPLE_DESCRIPTION_INDEX) { tfhd.defaultSampleDescriptionIndex = u32(b, p); p += 4; }
  if (flags & TFHD_DEFAULT_SAMPLE_DURATION) { tfhd.defaultSampleDuration = u32(b, p); p += 4; }
  if (flags & TFHD_DEFAULT_SAMPLE_SIZE) { tfhd.defaultSampleSize = u32(b, p); p += 4; }
  if (flags & TFHD_DEFAULT_SAMPLE_FLAGS) { tfhd.defaultSampleFlags = u32(b, p); p += 4; }
  if (flags & TFHD_DEFAULT_BASE_IS_MOOF) tfhd.defaultBaseIsMoof = b[p] !== 0;
  return tfhd;
}

export function parseTrun(b, box) {
  const flags = u32(b, box.dataStart) & 0xffffff;
  const version = b[box.dataStart];
  const sampleCount = u32(b, box.dataStart + 4);
  let p = box.dataStart + 8;
  const trun = { flags, version, sampleCount, dataOffset: 0, firstSampleFlags: 0, samples: [] };
  if (flags & TRUN_DATA_OFFSET) { trun.dataOffset = b[p] << 24 | b[p + 1] << 16 | b[p + 2] << 8 | b[p + 3]; p += 4; }
  if (flags & TRUN_FIRST_SAMPLE_FLAGS) { trun.firstSampleFlags = u32(b, p); p += 4; }
  for (let i = 0; i < sampleCount; i++) {
    const s = { duration: 0, size: 0, flags: 0, cto: 0 };
    if (flags & TRUN_SAMPLE_DURATION) { s.duration = u32(b, p); p += 4; }
    if (flags & TRUN_SAMPLE_SIZE) { s.size = u32(b, p); p += 4; }
    if (flags & TRUN_SAMPLE_FLAGS) { s.flags = u32(b, p); p += 4; }
    if (flags & TRUN_SAMPLE_CTO) {
      s.cto = version === 0 ? (b[p] << 24 | b[p + 1] << 16 | b[p + 2] << 8 | b[p + 3]) : (b[p] | (b[p + 1] << 8) | (b[p + 2] << 16) | (b[p + 3] << 24));
      p += 4;
    }
    trun.samples.push(s);
  }
  return trun;
}

// Parse the 'senc' box: per-sample IV plus optional subsample ranges.
export function parseSenc(b, box, perSampleIvSize) {
  // perSampleIvSize === 0 means the senc carries no IVs: every sample uses the
  // constant IV from the tenc box (that is how cbcs content is usually made).
  const flags = u32(b, box.dataStart) & 0xffffff;
  const sampleCount = u32(b, box.dataStart + 4);
  let p = box.dataStart + 8;
  const ivSize = perSampleIvSize;
  const samples = [];
  for (let i = 0; i < sampleCount; i++) {
    let iv = null;
    if (perSampleIvSize > 0) {
      iv = b.slice(p, p + perSampleIvSize);
      p += perSampleIvSize;
    }
    let subsamples = [];
    if (flags & 0x2) {
      const n = u16(b, p); p += 2;
      for (let j = 0; j < n; j++) {
        const clear = u16(b, p); p += 2;
        const enc = u32(b, p); p += 4;
        subsamples.push({ clear, enc });
      }
    }
    samples.push({ iv, subsamples });
  }
  return { flags, sampleCount, samples };
}

// Neutralise the auxiliary encryption boxes (senc/saiz/saio/pssh) in a moof.
//
// They are replaced by 'free' boxes of exactly the same length rather than
// removed, so the moof keeps its size: the trun data offsets and any sidx
// in the segment stay valid. Leaving a senc in place would make Chromium
// treat the track as encrypted, which it then refuses to decode.
export function stripAuxFromMoof(moofBuf, moofBox) {
  const children = parseBoxes(moofBuf, moofBox.dataStart, moofBox.end);
  const out = [];
  let changed = false;

  const neutralise = (buf, box) => {
    changed = true;
    return buildBox('free', new Uint8Array(Math.max(0, box.size - 8)));
  };

  for (const c of children) {
    if (c.type === 'senc' || c.type === 'saiz' || c.type === 'saio' || c.type === 'pssh') {
      out.push(neutralise(moofBuf, c));
      continue;
    }
    if (c.type === 'traf') {
      const trafChildren = parseBoxes(moofBuf, c.dataStart, c.end);
      const trafOut = [];
      let trafChanged = false;
      for (const tc of trafChildren) {
        if (tc.type === 'senc' || tc.type === 'saiz' || tc.type === 'saio') {
          trafOut.push(neutralise(moofBuf, tc));
          trafChanged = true;
          continue;
        }
        trafOut.push(moofBuf.slice(tc.start, tc.end));
      }
      if (trafChanged) {
        out.push(buildBox('traf', concat(trafOut)));
        continue;
      }
    }
    out.push(moofBuf.slice(c.start, c.end));
  }
  if (!changed) return moofBuf.slice(moofBox.start, moofBox.end);
  return buildBox('moof', concat(out));
}

// Parse 'sidx' into byte ranges of the referenced subsegments.
export function parseSidx(b, box, fileOffset) {
  const version = b[box.dataStart];
  const timescale = u32(b, box.dataStart + 12);
  let p = box.dataStart + 16;
  let earliest, firstOffset;
  if (version === 0) { earliest = u32(b, p); firstOffset = u32(b, p + 4); p += 8; }
  else { earliest = u64(b, p); firstOffset = u64(b, p + 8); p += 16; }
  p += 2; // reserved
  const count = u16(b, p); p += 2;
  const refs = [];
  let total = firstOffset;
  for (let i = 0; i < count; i++) {
    const typeAndSize = u32(b, p);
    const refType = (typeAndSize >>> 31) & 1;
    const size = typeAndSize & 0x7fffffff;
    const duration = u32(b, p + 4);
    p += 12;
    if (refType === 1) p += 4;
    refs.push({ size, duration, start: total });
    total += size;
  }
  const base = fileOffset + (box.start - fileOffset) + (box.end - box.start) - total + firstOffset;
  return { timescale, refs, earliest, base: fileOffset + firstOffset };
}

// ---------------------------------------------------------------------------
// Per-track split
//
// MSE wants one SourceBuffer per media track, and each SourceBuffer needs an
// init segment that contains only its own track. Real-world manifests are
// often multiplexed (HLS especially), so the init segment and the media
// segments both have to be split by track.
// ---------------------------------------------------------------------------

// track_ID of a trak (from tkhd).
export function getTrackId(b, trak) {
  const tkhd = findBoxes(b, 'tkhd', trak)[0];
  if (!tkhd) return null;
  const version = b[tkhd.dataStart];
  // version/flags(4) creation(4) modification(4) [creation(8) modification(8)]
  const offset = version === 1 ? 4 + 8 + 8 : 4 + 4 + 4;
  return u32(b, tkhd.dataStart + offset);
}

// 'vide' | 'soun' | ... of a trak (from hdlr).
export function getHandler(b, trak) {
  const hdlr = findBoxes(b, 'hdlr', trak)[0];
  if (!hdlr) return null;
  return fourcc(b, hdlr.dataStart + 8);
}

// Map track_ID -> 'video' | 'audio' for the init segment.
export function parseTrackMap(initBuf) {
  const map = new Map();
  const moov = parseBoxes(initBuf).find((b) => b.type === 'moov');
  if (!moov) return map;
  for (const trak of childBoxes(initBuf, moov)) {
    if (trak.type !== 'trak') continue;
    const id = getTrackId(initBuf, trak);
    const handler = getHandler(initBuf, trak);
    if (id === null || !handler) continue;
    const kind = handler === 'vide' ? 'video' : handler === 'soun' ? 'audio' : handler;
    map.set(id, kind);
  }
  return map;
}

// trex track_ID.
function getTrexTrackId(b, trex) {
  return u32(b, trex.dataStart + 4);
}

// Split an init segment into one init segment per media kind.
// Returns Map<'video'|'audio', Uint8Array>.
export function splitInitByTrack(initBuf) {
  const top = parseBoxes(initBuf);
  const moovBox = top.find((b) => b.type === 'moov');
  if (!moovBox) return null;
  const moovKids = parseBoxes(initBuf, moovBox.dataStart, moovBox.end);
  const mvex = moovKids.find((k) => k.type === 'mvex');
  const trexes = mvex ? parseBoxes(initBuf, mvex.dataStart, mvex.end).filter((k) => k.type === 'trex') : [];

  const groups = new Map();
  for (const trak of moovKids) {
    if (trak.type !== 'trak') continue;
    const id = getTrackId(initBuf, trak);
    const handler = getHandler(initBuf, trak);
    const kind = handler === 'vide' ? 'video' : handler === 'soun' ? 'audio' : handler;
    if (!groups.has(kind)) groups.set(kind, { traks: [], trackIds: [] });
    const g = groups.get(kind);
    g.traks.push(trak);
    if (id !== null) g.trackIds.push(id);
  }

  const result = new Map();
  for (const [kind, g] of groups) {
    const keepTrex = trexes.filter((t) => g.trackIds.includes(getTrexTrackId(initBuf, t)));
    const kids = [];
    for (const k of moovKids) {
      if (k.type === 'pssh') continue;
      if (k.type === 'trak') {
        if (g.traks.includes(k)) kids.push(initBuf.slice(k.start, k.end));
        continue;
      }
      if (k.type === 'mvex') {
        if (keepTrex.length) kids.push(buildBox('mvex', concat(keepTrex.map((t) => initBuf.slice(t.start, t.end)))));
        continue;
      }
      kids.push(initBuf.slice(k.start, k.end));
    }
    const newMoov = buildBox('moov', concat(kids));
    const parts = [];
    for (const b of top) {
      if (b.type === 'moov') parts.push(newMoov);
      else if (b.type !== 'pssh') parts.push(initBuf.slice(b.start, b.end));
    }
    result.set(kind, concat(parts));
  }
  return result;
}

// 24-bit flags field (FullBox flags are 3 bytes, not 4).
export function u24be(v) {
  return new Uint8Array([(v >>> 16) & 0xff, (v >>> 8) & 0xff, v & 0xff]);
}

export function u8(v) { return new Uint8Array([v & 0xff]); }
export function u32be(v) {
  const out = new Uint8Array(4);
  out[0] = (v >>> 24) & 0xff; out[1] = (v >>> 16) & 0xff;
  out[2] = (v >>> 8) & 0xff; out[3] = v & 0xff;
  return out;
}

// Serialize a tfhd (with default-base-is-moof forced on and base-data-offset
// cleared, because the rebuilt segment is self-contained).
export function buildTfhd(tfhd) {
  const flags = (tfhd.flags & ~0x000001) | 0x020000;
  const parts = [u8(0), u24be(flags), u32be(tfhd.trackId)];
  if (flags & 0x000002) parts.push(u32be(tfhd.defaultSampleDescriptionIndex ?? 1));
  if (flags & 0x000008) parts.push(u32be(tfhd.defaultSampleDuration));
  if (flags & 0x000010) parts.push(u32be(tfhd.defaultSampleSize));
  if (flags & 0x000020) parts.push(u32be(tfhd.defaultSampleFlags));
  parts.push(u8(1)); // default-base-is-moof
  return buildBox('tfhd', concat(parts));
}

// Serialize a trun with a new data_offset.
export function buildTrun(trun, dataOffset) {
  const flags = (trun.flags & ~0x000001) | 0x000001;
  const parts = [u8(trun.version), u24be(flags), u32be(trun.sampleCount), u32be(dataOffset)];
  if (flags & 0x000004) parts.push(u32be(trun.firstSampleFlags));
  for (const s of trun.samples) {
    if (flags & 0x000100) parts.push(u32be(s.duration));
    if (flags & 0x000200) parts.push(u32be(s.size));
    if (flags & 0x000400) parts.push(u32be(s.flags));
    if (flags & 0x000800) parts.push(u32be(s.cto >>> 0));
  }
  return buildBox('trun', concat(parts));
}

// Extract one track from a multiplexed media segment.
//
// Returns a segment that contains only the traf for `kind` ('video'/'audio'),
// with its samples copied into a fresh mdat. This is what a real HLS fMP4
// player has to do: MSE wants one SourceBuffer per track.
export function demuxSegment(segmentBuf, kind, trackMap) {
  const top = parseBoxes(segmentBuf);
  const moofBox = top.find((b) => b.type === 'moof');
  if (!moofBox) return null;
  const moofKids = parseBoxes(segmentBuf, moofBox.dataStart, moofBox.end);
  const mdatBox = top.find((b) => b.type === 'mdat');
  if (!mdatBox) return null;

  // Find the traf that carries the requested track.
  let target = null;
  for (const k of moofKids) {
    if (k.type !== 'traf') continue;
    const kids = parseBoxes(segmentBuf, k.dataStart, k.end);
    const tfhdBox = kids.find((x) => x.type === 'tfhd');
    if (!tfhdBox) continue;
    const tfhd = parseTfhd(segmentBuf, tfhdBox);
    if (trackMap.get(tfhd.trackId) === kind) { target = { box: k, kids, tfhd }; break; }
  }
  if (!target) return null;

  const { box: traf, kids, tfhd } = target;
  const trunBoxes = kids.filter((x) => x.type === 'trun');
  const truns = trunBoxes.map((x) => parseTrun(segmentBuf, x));
  const base = tfhd.defaultBaseIsMoof ? moofBox.start : (tfhd.baseDataOffset ?? moofBox.start);

  // Gather the sample bytes for this track and the offset of each trun
  // inside the track's own mdat payload.
  const pieces = [];
  const trunOffsets = [];
  let offsetInTrack = 0;
  for (const trun of truns) {
    let pos = base + trun.dataOffset;
    trunOffsets.push(offsetInTrack);
    for (const s of trun.samples) {
      const size = s.size || tfhd.defaultSampleSize;
      pieces.push(segmentBuf.slice(pos, pos + size));
      pos += size;
      offsetInTrack += size;
    }
  }
  const mdatPayload = concat(pieces);

  // Rebuild the traf: tfhd (new base), tfdt (unchanged), trun (new offsets).
  const newTrafKids = [];
  let trunIdx = 0;
  for (const k of kids) {
    if (k.type === 'tfhd') { newTrafKids.push(buildTfhd(tfhd)); continue; }
    if (k.type === 'trun') { newTrafKids.push(null); trunIdx++; continue; } // patched below
    newTrafKids.push(segmentBuf.slice(k.start, k.end));
  }

  // The moof size does not depend on the offset values, so build it once to
  // measure it, then patch the trun offsets.
  const buildMoof = (patched) => buildBox('moof', concat(moofKids.map((k) => {
    if (k.type !== 'traf') return segmentBuf.slice(k.start, k.end);
    if (k !== target.box) return new Uint8Array(0); // drop the other tracks
    return buildBox('traf', concat(patched));
  })));

  const placeholder = newTrafKids.map((k) => (k === null ? buildTrun(truns[0], 0) : k));
  const moofSize = buildMoof(placeholder).length;

  const patched = [];
  let ti = 0;
  for (const k of newTrafKids) {
    if (k === null) {
      patched.push(buildTrun(truns[ti], moofSize + 8 + trunOffsets[ti]));
      ti++;
      continue;
    }
    patched.push(k);
  }

  const newMoof = buildMoof(patched);
  const newMdat = buildBox('mdat', mdatPayload);
  return concat([newMoof, newMdat]);
}

// Shift every fragment's baseMediaDecodeTime by `delta` (in the track's own
// timescale units).
//
// A live manifest often describes its window with a timeline that restarts at
// 0 (the window is relative to its own start). Appending such a window after
// the previous one would make the media jump backwards, so the fragments are
// re-stamped to continue where the previous window ended.
export function shiftFragmentTimes(buffer, delta) {
  if (!delta) return buffer;
  const out = buffer;
  for (const moof of parseBoxes(out).filter((b) => b.type === 'moof')) {
    for (const traf of parseBoxes(out, moof.dataStart, moof.end).filter((b) => b.type === 'traf')) {
      for (const tfdt of parseBoxes(out, traf.dataStart, traf.end).filter((b) => b.type === 'tfdt')) {
        const d = tfdt.dataStart;
        const version = out[d];
        if (version === 1) {
          const cur = u64(out, d + 4);
          const next = BigInt(Math.round(cur + delta));
          out.set([Number((next >> 32n) & 0xffffffffn), Number(next & 0xffffffffn)], d + 4);
        } else {
          const cur = u32(out, d + 4);
          out.set([(cur + Math.round(delta)) >>> 24 & 0xff, (cur + Math.round(delta)) >>> 16 & 0xff,
                   (cur + Math.round(delta)) >>> 8 & 0xff, (cur + Math.round(delta)) & 0xff], d + 4);
        }
      }
    }
  }
  return out;
}

// Remove the top-level 'sidx' boxes from a media segment.
//
// A sidx describes the segment's own (absolute) times. When the fragments
// are re-stamped to continue a previous window, the sidx no longer matches,
// and Chromium rejects the segment. The index is optional for playback, so it
// is dropped; the trun offsets are relative to the moof and stay valid.
export function stripSidx(buffer) {
  const boxes = parseBoxes(buffer);
  if (!boxes.some((b) => b.type === 'sidx')) return buffer;
  const parts = [];
  let cursor = 0;
  for (const b of boxes) {
    if (b.type === 'sidx') {
      if (b.start > cursor) parts.push(buffer.slice(cursor, b.start));
      cursor = b.end;
    }
  }
  if (cursor < buffer.length) parts.push(buffer.slice(cursor));
  return concat(parts);
}

// The absolute media time of every fragment in a segment:
// [{ start, end }] — the tfdt base plus the sample durations of the trun.
export function fragmentTimes(buffer) {
  const out = [];
  for (const moof of parseBoxes(buffer).filter((b) => b.type === 'moof')) {
    for (const traf of parseBoxes(buffer, moof.dataStart, moof.end).filter((b) => b.type === 'traf')) {
      const kids = parseBoxes(buffer, traf.dataStart, traf.end);
      const tfdt = kids.find((k) => k.type === 'tfdt');
      if (!tfdt) continue;
      const d = tfdt.dataStart;
      const base = buffer[d] === 1 ? Number(u64(buffer, d + 4)) : u32(buffer, d + 4);
      // The sample durations may live in the tfhd's default instead of the
      // trun; without them the fragment's end time would be wrong.
      const tfhdBox = kids.find((k) => k.type === 'tfhd');
      const defDur = tfhdBox ? (parseTfhd(buffer, tfhdBox).defaultSampleDuration || 0) : 0;
      let dur = 0;
      for (const trunBox of kids.filter((k) => k.type === 'trun')) {
        const trun = parseTrun(buffer, trunBox);
        let total = trun.samples.reduce((a, sm) => a + (sm.duration || 0), 0);
        if (!total) total = defDur * trun.samples.length;
        dur += total;
      }
      out.push({ start: base, end: base + dur });
    }
  }
  return out;
}
