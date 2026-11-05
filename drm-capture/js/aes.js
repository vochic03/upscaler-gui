// aes.js — a compact AES block cipher (128/192/256 bit keys).
//
// Why: some browser builds refuse `crypto.subtle.decrypt` with AES-CBC (an
// Electron/Chromium quirk was observed: AES-CBC *encrypt* works, *decrypt*
// throws OperationError). CENC's cbcs scheme needs CBC decryption, so the
// player feature-tests WebCrypto and falls back to this implementation.
//
// Validated against the FIPS-197 test vectors:
//   key 000102030405060708090a0b0c0d0e0f
//   plain 00112233445566778899aabbccddeeff
//   cipher 69c4e0d86a7b0430d8cdb78070b4c55a

function xtime(a) {
  return ((a << 1) ^ ((a & 0x80) ? 0x1b : 0)) & 0xff;
}

function mul(a, b) {
  let r = 0;
  while (b) {
    if (b & 1) r ^= a;
    a = xtime(a);
    b >>= 1;
  }
  return r & 0xff;
}

function buildTables() {
  // Multiplicative inverses in GF(2^8)
  const inv = new Uint8Array(256);
  for (let i = 1; i < 256; i++) {
    for (let j = 1; j < 256; j++) {
      if (mul(i, j) === 1) { inv[i] = j; break; }
    }
  }
  const sbox = new Uint8Array(256);
  const invSbox = new Uint8Array(256);
  for (let i = 0; i < 256; i++) {
    const b = inv[i];
    const s = b ^ rotl(b, 1) ^ rotl(b, 2) ^ rotl(b, 3) ^ rotl(b, 4) ^ 0x63;
    sbox[i] = s & 0xff;
    invSbox[s & 0xff] = i;
  }
  return { sbox, invSbox };
}

function rotl(b, n) {
  return ((b << n) | (b >> (8 - n))) & 0xff;
}

const { sbox: SBOX, invSbox: INV_SBOX } = buildTables();
const RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36, 0x6c, 0xd8, 0xab, 0x4d];

export class AES {
  constructor(key) {
    if (key.length !== 16 && key.length !== 24 && key.length !== 32) {
      throw new Error(`AES key must be 16, 24 or 32 bytes, got ${key.length}`);
    }
    this.nk = key.length / 4;
    this.nr = this.nk + 6;
    this.w = this.expandKey(key);
  }

  expandKey(key) {
    const nk = this.nk;
    const nr = this.nr;
    const w = new Array(4 * (nr + 1));
    for (let i = 0; i < nk; i++) {
      w[i] = [key[4 * i], key[4 * i + 1], key[4 * i + 2], key[4 * i + 3]];
    }
    for (let i = nk; i < 4 * (nr + 1); i++) {
      let t = w[i - 1].slice();
      if (i % nk === 0) {
        t = [SBOX[t[1]] ^ RCON[i / nk - 1], SBOX[t[2]], SBOX[t[3]], SBOX[t[0]]];
      } else if (nk > 6 && i % nk === 4) {
        t = t.map((x) => SBOX[x]);
      }
      const prev = w[i - nk];
      w[i] = [prev[0] ^ t[0], prev[1] ^ t[1], prev[2] ^ t[2], prev[3] ^ t[3]];
    }
    return w;
  }

  addRoundKey(s, round) {
    for (let c = 0; c < 4; c++) {
      const word = this.w[round * 4 + c];
      for (let r = 0; r < 4; r++) s[r + 4 * c] ^= word[r];
    }
  }

  encryptBlock(input) {
    const s = Uint8Array.from(input.subarray(0, 16));
    this.addRoundKey(s, 0);
    for (let round = 1; round < this.nr; round++) {
      for (let i = 0; i < 16; i++) s[i] = SBOX[s[i]];
      shiftRows(s);
      mixColumns(s);
      this.addRoundKey(s, round);
    }
    for (let i = 0; i < 16; i++) s[i] = SBOX[s[i]];
    shiftRows(s);
    this.addRoundKey(s, this.nr);
    return s;
  }

  decryptBlock(input) {
    const s = Uint8Array.from(input.subarray(0, 16));
    this.addRoundKey(s, this.nr);
    for (let round = this.nr - 1; round >= 1; round--) {
      invShiftRows(s);
      for (let i = 0; i < 16; i++) s[i] = INV_SBOX[s[i]];
      this.addRoundKey(s, round);
      invMixColumns(s);
    }
    invShiftRows(s);
    for (let i = 0; i < 16; i++) s[i] = INV_SBOX[s[i]];
    this.addRoundKey(s, 0);
    return s;
  }
}

function shiftRows(s) {
  let t;
  t = s[1]; s[1] = s[5]; s[5] = s[9]; s[9] = s[13]; s[13] = t;         // row 1 <<< 1
  t = s[2]; s[2] = s[10]; s[10] = t;
  t = s[6]; s[6] = s[14]; s[14] = t;                                  // row 2 <<< 2
  t = s[15]; s[15] = s[11]; s[11] = s[7]; s[7] = s[3]; s[3] = t;       // row 3 <<< 3
}

function invShiftRows(s) {
  let t;
  t = s[13]; s[13] = s[9]; s[9] = s[5]; s[5] = s[1]; s[1] = t;         // row 1 >>> 1
  t = s[2]; s[2] = s[10]; s[10] = t;
  t = s[6]; s[6] = s[14]; s[14] = t;                                  // row 2 >>> 2
  t = s[3]; s[3] = s[7]; s[7] = s[11]; s[11] = s[15]; s[15] = t;       // row 3 >>> 3
}

function mixColumns(s) {
  for (let c = 0; c < 4; c++) {
    const i = 4 * c;
    const a0 = s[i], a1 = s[i + 1], a2 = s[i + 2], a3 = s[i + 3];
    s[i] = xtime(a0) ^ (xtime(a1) ^ a1) ^ a2 ^ a3;
    s[i + 1] = a0 ^ xtime(a1) ^ (xtime(a2) ^ a2) ^ a3;
    s[i + 2] = a0 ^ a1 ^ xtime(a2) ^ (xtime(a3) ^ a3);
    s[i + 3] = (xtime(a0) ^ a0) ^ a1 ^ a2 ^ xtime(a3);
  }
}

function invMixColumns(s) {
  for (let c = 0; c < 4; c++) {
    const i = 4 * c;
    const a0 = s[i], a1 = s[i + 1], a2 = s[i + 2], a3 = s[i + 3];
    s[i] = mul(a0, 14) ^ mul(a1, 11) ^ mul(a2, 13) ^ mul(a3, 9);
    s[i + 1] = mul(a0, 9) ^ mul(a1, 14) ^ mul(a2, 11) ^ mul(a3, 13);
    s[i + 2] = mul(a0, 13) ^ mul(a1, 9) ^ mul(a2, 14) ^ mul(a3, 11);
    s[i + 3] = mul(a0, 11) ^ mul(a1, 13) ^ mul(a2, 9) ^ mul(a3, 14);
  }
}

// --- modes on top of the block cipher -------------------------------------

// CBC decryption: P_i = D(C_i) XOR C_{i-1}. The trailing partial block is
// left untouched (matches ffmpeg's av_aes_crypt(..., size/16, ...)).
export function cbcDecryptBlocks(aes, iv, data) {
  const out = new Uint8Array(data.length);
  const whole = data.length - (data.length % 16);
  let prev = iv.slice(0, 16);
  for (let off = 0; off < whole; off += 16) {
    const cipher = data.subarray(off, off + 16);
    const plain = aes.decryptBlock(cipher);
    for (let i = 0; i < 16; i++) out[off + i] = plain[i] ^ prev[i];
    prev = cipher;
  }
  if (whole < data.length) out.set(data.subarray(whole), whole);
  return out;
}

// CTR mode: keystream = E(counter), counter advances on the low 64 bits.
export function ctrCryptBlocks(aes, counter, data) {
  const out = new Uint8Array(data.length);
  const ctr = counter.slice(0, 16);
  for (let off = 0; off < data.length; off += 16) {
    const ks = aes.encryptBlock(ctr);
    const n = Math.min(16, data.length - off);
    for (let i = 0; i < n; i++) out[off + i] = data[off + i] ^ ks[i];
    // increment the low 64 bits, big-endian
    for (let i = 15; i >= 8; i--) {
      ctr[i] = (ctr[i] + 1) & 0xff;
      if (ctr[i] !== 0) break;
    }
  }
  return out;
}
