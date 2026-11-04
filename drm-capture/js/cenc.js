// cenc.js — Common Encryption decryption in the browser (WebCrypto).
//
// Semantics follow ISO/IEC 23001-7 and match ffmpeg's libavformat/mov.c
// (cenc_scheme_decrypt / cbcs_scheme_decrypt / cens_scheme_decrypt), which
// is the reference this project is validated against:
//
//   cenc  : AES-CTR, counter = 16-byte block, only the low 64 bits increment
//           (big-endian), one increment per 16-byte block consumed, including
//           a trailing partial block. Counter restarts at the sample IV.
//   cens  : AES-CTR restricted to the 'crypt' blocks of the pattern; the
//           counter still advances only over crypt blocks.
//   cbcs  : AES-CBC restricted to the 'crypt' blocks of the pattern. The IV
//           for the first crypt block of a subsample range is the sample IV;
//           for later crypt blocks it is the previous crypt block's
//           ciphertext (standard CBC chaining). The IV is reset to the
//           sample IV at the start of every subsample range.
//   cbc1  : AES-CBC over the whole sample.

import {
  u32, u16, hex, unhex,
  parseBoxes, childBoxes, parseTfhd, parseTrun, parseSenc, stripAuxFromMoof,
} from './boxes.js';
import { AES, cbcDecryptBlocks, ctrCryptBlocks } from './aes.js';

export const SCHEME_CTR = 'cenc';
export const SCHEME_CBC = 'cbcs';

// --- tenc -----------------------------------------------------------------

// Parse a 'tenc' box.
//
// Layout (ISO/IEC 14496-12 §8.16.3, matching ffmpeg's mov_read_tenc):
//   version(1) flags(3) reserved(1) pattern(1) is_protected(1) iv_size(1)
//   KID(16) [ constant_IV_size(1) constant_IV ] when protected && !iv_size
// For version > 0 the 'pattern' byte carries the cbcs/cens block pattern:
//   crypt_byte_block = pattern >> 4, skip_byte_block = pattern & 0x0f
export function parseTenC(b, box) {
  const version = b[box.dataStart];
  const flags = u32(b, box.dataStart) & 0xffffff;
  let p = box.dataStart + 4;
  p += 1; // reserved
  const patternByte = b[p]; p += 1;
  let cryptByteBlock = 0;
  let skipByteBlock = 0;
  if (version > 0) {
    cryptByteBlock = (patternByte >> 4) & 0x0f;
    skipByteBlock = patternByte & 0x0f;
  }
  const isProtected = b[p]; p += 1;
  const perSampleIvSize = b[p]; p += 1;
  const defaultKid = hex(b.slice(p, p + 16)); p += 16;
  let constantIv = null;
  if (isProtected && perSampleIvSize === 0) {
    const ivSize = b[p]; p += 1;
    if (ivSize > 0) constantIv = hex(b.slice(p, p + ivSize));
  }
  return {
    version, flags, isProtected, perSampleIvSize, defaultKid,
    constantIv, cryptByteBlock, skipByteBlock,
    pattern: (cryptByteBlock || skipByteBlock) ? { cryptByteBlock, skipByteBlock } : null,
  };
}

// Build the 16-byte counter block / CBC IV from a per-sample IV.
//
// A CENC IV is 8 bytes for 'cenc'/'cbcs'; the block counter is then
// IV || 0x0000000000000000 (ffmpeg copies the 8-byte IV into the high half
// of a zeroed 16-byte buffer). A 16-byte IV is used as-is.
export function ivToBlock(ivBytes) {
  const block = new Uint8Array(16);
  if (ivBytes.length >= 16) block.set(ivBytes.slice(0, 16), 0);
  else block.set(ivBytes, 0); // left-aligned: 8-byte IV lands in the high half
  return block;
}

// --- counter helpers ------------------------------------------------------

// Increment the low 64 bits of a 16-byte counter, big-endian (ffmpeg's
// av_aes_ctr_increment_be64 on counter + 8).
export function ctrAdvance(counter, blocks) {
  let carry = BigInt(blocks);
  for (let i = 15; i >= 8 && carry > 0n; i--) {
    const sum = BigInt(counter[i]) + (carry & 0xffn);
    counter[i] = Number(sum & 0xffn);
    carry = (carry >> 8n) + (sum >> 8n);
  }
  return counter;
}

// --- key helpers ----------------------------------------------------------

// Some browser builds refuse crypto.subtle.decrypt for AES-CBC (an
// Electron/Chromium quirk: AES-CBC encrypt works, decrypt throws
// OperationError). Detect what actually works and keep a pure-JS AES
// implementation as the fallback.
const caps = { ctr: false, cbc: false };

async function detectCaps() {
  if (caps.ctr || caps.cbc) return caps;
  try {
    const k = await crypto.subtle.importKey('raw', new Uint8Array(16), { name: 'AES-CTR' }, false, ['decrypt']);
    await crypto.subtle.decrypt({ name: 'AES-CTR', counter: new Uint8Array(16), length: 64 }, k, new Uint8Array(16));
    caps.ctr = true;
  } catch { caps.ctr = false; }
  try {
    const k = await crypto.subtle.importKey('raw', new Uint8Array(16), { name: 'AES-CBC' }, false, ['decrypt']);
    await crypto.subtle.decrypt({ name: 'AES-CBC', iv: new Uint8Array(16) }, k, new Uint8Array(16));
    caps.cbc = true;
  } catch { caps.cbc = false; }
  return caps;
}

export async function importKeys(keyHex) {
  const raw = unhex(keyHex);
  if (raw.length !== 16) throw new Error(`content key must be 16 bytes (32 hex chars), got ${raw.length}`);
  await detectCaps();
  const ctr = caps.ctr
    ? await crypto.subtle.importKey('raw', raw, { name: 'AES-CTR' }, false, ['decrypt'])
    : null;
  const cbc = caps.cbc
    ? await crypto.subtle.importKey('raw', raw, { name: 'AES-CBC' }, false, ['decrypt'])
    : null;
  return { ctr, cbc, aes: new AES(raw), caps };
}

async function ctrDecrypt(key, counter, data) {
  if (!data.length) return new Uint8Array(0);
  if (!key) throw new Error('WebCrypto AES-CTR decrypt is unavailable in this browser');
  const out = await crypto.subtle.decrypt(
    { name: 'AES-CTR', counter: counter.slice().buffer, length: 64 },
    key,
    data.slice().buffer,
  );
  return new Uint8Array(out);
}

async function cbcDecrypt(key, iv, data) {
  if (!data.length) return new Uint8Array(0);
  if (data.length % 16 !== 0) {
    throw new Error(`AES-CBC: data length ${data.length} is not a multiple of 16`);
  }
  if (iv.length !== 16) {
    throw new Error(`AES-CBC: iv length ${iv.length} (expected 16)`);
  }
  if (!key) throw new Error('WebCrypto AES-CBC decrypt is unavailable in this browser');
  const out = await crypto.subtle.decrypt(
    { name: 'AES-CBC', iv: iv.slice().buffer },
    key,
    data.slice().buffer,
  );
  return new Uint8Array(out);
}

// --- sample decryption ----------------------------------------------------

// Decrypt one sample in place.
//
// sampleBytes : the sample payload (as stored in mdat)
// iv          : 16-byte per-sample IV (or the constant IV)
// subsamples  : [{clear, enc}] ranges; null/empty => whole sample encrypted
// scheme      : 'cenc' | 'cens' | 'cbcs' | 'cbc1'
// pattern     : {cryptByteBlock, skipByteBlock} or null
export async function decryptSample(sampleBytes, iv, subsamples, scheme, pattern, keys) {
  const src = sampleBytes;
  const out = new Uint8Array(src.length);
  out.set(src);
  if (!iv) return out;

  const ivBytes = unhex(iv);
  const counter = ivToBlock(ivBytes);

  const ranges = (subsamples && subsamples.length)
    ? subsamples
    : [{ clear: 0, enc: src.length }];

  if (scheme === 'cbc1' || scheme === 'cbcs') {
    for (const r of ranges) {
      let pos = r.clear;
      let end = r.clear + r.enc;
      if (end > out.length) end = out.length;
      if (!pattern) {
        // No pattern: plain CBC over the whole range. Only complete blocks
        // are decrypted — a trailing partial block stays as it is, which is
        // what ffmpeg does (av_aes_crypt(..., size/16, ...)).
        const len = end - pos;
        const whole = len - (len % 16);
        if (whole > 0) {
          const chunk = out.slice(pos, pos + whole);
          const plain = keys.cbc
            ? await cbcDecrypt(keys.cbc, counter, chunk)
            : cbcDecryptBlocks(keys.aes, counter, chunk);
          out.set(plain, pos);
        }
        continue;
      }
      // cbcs pattern
      const runIv = counter.slice(); // reset per subsample range
      while (pos < end) {
        const cryptLen = 16 * pattern.cryptByteBlock;
        if (end - pos < cryptLen) break; // trailing partial block stays clear
        const cipher = out.slice(pos, pos + cryptLen);
        const plain = keys.cbc
          ? await cbcDecrypt(keys.cbc, runIv, cipher)
          : cbcDecryptBlocks(keys.aes, runIv, cipher);
        out.set(plain, pos);
        // next crypt run chains from the last ciphertext block of this run
        runIv.set(cipher.slice(cryptLen - 16, cryptLen));
        pos += cryptLen;
        const skip = Math.min(16 * pattern.skipByteBlock, end - pos);
        pos += skip;
      }
    }
    return out;
  }

  // AES-CTR (cenc / cens)
  for (const r of ranges) {
    let pos = r.clear;
    let end = r.clear + r.enc;
    if (end > out.length) end = out.length;
    if (!pattern) {
      const chunk = out.slice(pos, end);
      const plain = keys.ctr
        ? await ctrDecrypt(keys.ctr, counter, chunk)
        : ctrCryptBlocks(keys.aes, counter, chunk);
      out.set(plain, pos);
      // ffmpeg's av_aes_ctr_crypt consumes one counter value per 16-byte
      // block, including a trailing partial block.
      ctrAdvance(counter, Math.ceil((end - pos) / 16));
      continue;
    }
    // cens: CTR keystream applied only to the crypt blocks
    while (pos < end) {
      const cryptLen = 16 * pattern.cryptByteBlock;
      if (end - pos < cryptLen) break;
      const chunk = out.slice(pos, pos + cryptLen);
      const plain = keys.ctr
        ? await ctrDecrypt(keys.ctr, counter, chunk)
        : ctrCryptBlocks(keys.aes, counter, chunk);
      out.set(plain, pos);
      ctrAdvance(counter, pattern.cryptByteBlock);
      pos += cryptLen;
      const skip = Math.min(16 * pattern.skipByteBlock, end - pos);
      pos += skip;
    }
  }
  return out;
}

// --- HLS full-segment AES-128 --------------------------------------------

// EXT-X-KEY:METHOD=AES-128 — the whole segment is AES-128-CBC encrypted
// with the key from the playlist and an IV that is either explicit or the
// media sequence number as a 16-byte big-endian value.
export async function decryptAes128(bytes, keyBytes, ivBytes) {
  const key = await crypto.subtle.importKey('raw', keyBytes.slice(0, 16), { name: 'AES-CBC' }, false, ['decrypt']);
  const out = await crypto.subtle.decrypt(
    { name: 'AES-CBC', iv: ivBytes.slice(0, 16).buffer },
    key,
    bytes.slice().buffer,
  );
  return new Uint8Array(out);
}

// --- segment decryption ---------------------------------------------------

// Decrypt a fragmented media segment in place.
//
// Works on the whole segment buffer: finds the moof, walks traf/trun to get
// each sample's byte range, and applies the per-sample IV + subsample ranges
// from the senc box.
//
// Returns { buffer, samples } where `buffer` is the segment with decrypted
// samples and with the auxiliary encryption boxes removed.
export async function decryptSegment(segmentBuf, tenc, scheme, keys, opts = {}) {
  const out = segmentBuf.slice();
  const perSampleIvSize = tenc ? tenc.perSampleIvSize : 0;
  const constantIv = tenc && tenc.constantIv ? tenc.constantIv : null;
  let decryptedSamples = 0;
  let anySenc = false;

  // A segment file can hold several fragments (this CDN packs four
  // moof/mdat pairs into one .m4s), so every moof has to be handled.
  const moofs = parseBoxes(out).filter((b) => b.type === 'moof');

  for (const moofBox of moofs) {
    const moofKids = parseBoxes(out, moofBox.dataStart, moofBox.end);
    const moofSencBox = moofKids.find((c) => c.type === 'senc');
    let sharedIndex = 0;

    for (const traf of moofKids.filter((c) => c.type === 'traf')) {
      const kids = parseBoxes(out, traf.dataStart, traf.end);
      const tfhdBox = kids.find((x) => x.type === 'tfhd');
      if (!tfhdBox) continue;
      const tfhd = parseTfhd(out, tfhdBox);
      const truns = kids.filter((x) => x.type === 'trun').map((x) => parseTrun(out, x));

      const trafSencBox = kids.find((x) => x.type === 'senc');
      const sencBox = trafSencBox || moofSencBox;
      if (!sencBox) continue;
      anySenc = true;
      const senc = parseSenc(out, sencBox, perSampleIvSize);
      const shared = !trafSencBox && !!moofSencBox;

      const base = tfhd.defaultBaseIsMoof ? moofBox.start : (tfhd.baseDataOffset ?? moofBox.start);
      let sampleIndex = shared ? sharedIndex : 0;

      for (const trun of truns) {
        let dataPos = base + trun.dataOffset;
        for (const s of trun.samples) {
          const size = s.size || tfhd.defaultSampleSize;
          const info = senc.samples[sampleIndex];
          const iv = info && info.iv ? hex(info.iv) : constantIv;
          const subsamples = info ? info.subsamples : null;
          if (size > 0 && iv) {
            const plain = await decryptSample(
              out.slice(dataPos, dataPos + size), iv, subsamples, scheme,
              tenc ? tenc.pattern : null, keys);
            out.set(plain, dataPos);
            decryptedSamples++;
          }
          dataPos += size;
          sampleIndex++;
        }
      }
      if (shared) sharedIndex = sampleIndex;
    }

    // Neutralise this moof's aux boxes. The replacement has the same length,
    // so every offset in the segment stays valid.
    const stripped = stripAuxFromMoof(out, moofBox);
    out.set(stripped, moofBox.start);
  }

  return { buffer: out, samples: decryptedSamples, sencFound: anySenc };
}
