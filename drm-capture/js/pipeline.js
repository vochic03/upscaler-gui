// pipeline.js — the actual work, independent of the UI.
//
// load()  -> resolve manifest, fetch + clear the init segment(s), register
//            one SourceBuffer per media track, then feed decrypted segments.
// dump()  -> optional callback that receives every byte we produced, so the
//            output can be diffed against ffmpeg's own decryption.

import { ClearPlayer } from './player.js';
import { decryptSegment, importKeys, decryptAes128, parseTenC } from './cenc.js';
import { parseDash, parseM3u8, splitFragmentedMp4 } from './manifest.js';
import { makeClearInit, splitInitByTrack, demuxSegment, parseTrackMap, fragmentTimes } from './boxes.js';

export class Pipeline {
  constructor(videoEl, { log = () => {}, dump = null } = {}) {
    this.video = videoEl;
    this.log = log;
    this.dump = dump;
    this.player = new ClearPlayer(videoEl, log);
    this.crypto = new Map();   // track kind -> { scheme, tenc }
    this.keys = null;
    this.headers = {};
    this.hlsKeys = new Map();
    this.appended = new Map();  // kind -> Set of segment URLs already fed
    this.lastStart = new Map(); // kind -> media time of the last fed segment
    this.timelineEnd = new Map(); // kind -> absolute media time fed so far
    this.lastNumber = new Map();  // kind -> last segment number fed
    this.segInfo = new Map();     // kind -> segment template info
    this.nextTime = new Map();    // kind -> media time the next segment must start at
    this.windowBase = new Map();  // kind -> first segment time of the current window
    this.liveTimer = null;
    this.nudgeTimer = null;
    this.refreshing = false;
    this.stopped = false;
    this.liveRefreshMs = 8000;
    this.followLive = false;
    this.tracks = [];
  }

  async proxyFetch(url, headers = this.headers, as = 'bytes') {
    const res = await fetch('/proxy', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, headers }),
    });
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      throw new Error(`HTTP ${res.status} ${text.slice(0, 200)}`);
    }
    if (as === 'text') return await res.text();
    return new Uint8Array(await res.arrayBuffer());
  }

  // --- manifest resolution ----------------------------------------------

  async resolveSource(url) {
    const text = await this.proxyFetch(url, this.headers, 'text');

    if (/^\s*<\?xml/i.test(text) || /^\s*<MPD/i.test(text) || /\.mpd(\?|$)/i.test(url)) {
      const dash = parseDash(text, url);
      return {
        kind: 'dash',
        isLive: dash.isLive,
        publishTime: dash.publishTime,
        availabilityStartTime: dash.availabilityStartTime,
        tracks: dash.tracks.map((t) => ({
          kind: t.kind,
          initUrl: t.init ? new URL(t.init, url).href : null,
          segments: t.segments.map((s) => ({
            url: new URL(s.url, url).href,
            start: s.start,
            duration: s.duration,
          })),
          mimeType: t.mimeType,
          mediaTemplate: t.mediaTemplate,
          base: t.base,
          rid: t.rid,
          timescale: t.timescale || 1,
        })),
      };
    }

    if (text.includes('#EXTM3U')) {
      let pl = parseM3u8(text);
      if (pl.master) {
        const variant = pl.variants.slice().sort((a, b) => b.bandwidth - a.bandwidth)[0];
        const vurl = new URL(variant.url, url).href;
        this.log(`master playlist -> variant ${variant.bandwidth} bps`);
        pl = parseM3u8(await this.proxyFetch(vurl, this.headers, 'text'));
        pl.base = vurl;
      }
      const base = pl.base || url;
      return {
        kind: 'hls',
        tracks: [{
          kind: 'multiplexed',
          initUrl: pl.init ? new URL(pl.init, base).href : null,
          segments: pl.segments.map((s, i) => ({
            ...s,
            url: new URL(s.url, base).href,
            start: s.start !== undefined ? s.start : i,
          })),
        }],
      };
    }

    // Not a manifest: maybe a fragmented MP4.
    if (/\.mp4(\?|$)/i.test(url)) {
      const file = await this.proxyFetch(url, this.headers);
      const split = splitFragmentedMp4(file);
      return {
        kind: 'file',
        tracks: [{
          kind: 'multiplexed',
          initBytes: split.init,
          segments: split.segments.map((b) => ({ bytes: b })),
        }],
      };
    }
    throw new Error('unrecognised manifest format');
  }

  // --- loading ------------------------------------------------------------

  async load(manifestUrl, { headers = {}, keyHex = '', schemeOverride = '' } = {}) {
    this.headers = headers;
    if (keyHex) this.keys = await importKeys(keyHex);
    else this.keys = null;

    this.manifestUrl = manifestUrl;
    this.appended = new Map();
    const source = await this.resolveSource(manifestUrl);
    this.isLive = source.isLive;
    // The manifest's publishTime is the *catch-up position* - i.e. the live
    // edge minus the requested delay - not the live edge itself. The true
    // live edge is that plus the delay the manifest asked for.
    this.publishTime = source.publishTime ? Date.parse(source.publishTime) : Date.now();
    let delayParam = 0;
    try {
      delayParam = Number(new URL(manifestUrl).searchParams.get('delay') || 0) || 0;
    } catch { /* not a URL we can parse */ }
    this.catchUpDelay = delayParam;
    this.liveEdge = this.publishTime + delayParam * 1000;
    this.log(`source: ${source.kind}, tracks: ${source.tracks.map((t) => t.kind).join(', ')}, ${this.isLive ? 'live' : 'VOD'}`);

    await this.player.open();
    this.tracks = source.tracks;

    for (const track of source.tracks) {
      await this.setupTrack(track, schemeOverride);
      if (track.kind && track.kind !== 'multiplexed') {
        const segs = track.segments || [];
        const last = segs.length ? segs[segs.length - 1] : null;
        const nums = segs.map((sg) => segmentNumber(sg.url)).filter((n) => n !== null);
        if (nums.length) this.lastNumber.set(track.kind, Math.max(...nums));
        if (track.mediaTemplate) {
          this.segInfo.set(track.kind, {
            mediaTemplate: track.mediaTemplate, base: track.base, rid: track.rid,
          });
        }
      }
    }
    // All SourceBuffers must exist before any init segment is appended:
    // Chromium caps the number of SourceBuffers at the number of tracks in
    // the first init segment it receives.
    await this.player.commitInits();
    return source;
  }

  async setupTrack(track, schemeOverride) {
    if (track.kind === 'multiplexed') {
      this.log(`  multiplexed: init ${track.initUrl}`);
      const initBytes = track.initBytes || await this.proxyFetch(track.initUrl, this.headers);
      this.log(`  multiplexed: init fetched (${initBytes.length} bytes)`);
      const split = splitInitByTrack(initBytes);
      if (!split) throw new Error('could not split the init segment by track');
      track.splitKinds = Array.from(split.keys());
      track.trackMap = parseTrackMap(initBytes);
      for (const [kind, part] of split) {
        const cleared = makeClearInit(part);
        this.registerCrypto(kind, cleared, schemeOverride, part);
        await this.player.addTrack(kind, cleared.init);
        await this.emitDump(`init-${kind}.bin`, cleared.init);
      }
      return;
    }

      this.log(`  ${track.kind}: init ${track.initUrl}`);
    const initBytes = await this.proxyFetch(track.initUrl, this.headers);
      this.log(`  ${track.kind}: init fetched (${initBytes.length} bytes)`);
    const cleared = makeClearInit(initBytes);
    this.registerCrypto(track.kind, cleared, schemeOverride, initBytes);
    await this.player.addTrack(track.kind, cleared.init);
    await this.emitDump(`init-${track.kind}.bin`, cleared.init);
  }

  registerCrypto(kind, cleared, schemeOverride, sourceBuf) {
    const scheme = schemeOverride || cleared.scheme;
    const tenc = cleared.tencBox ? parseTenC(sourceBuf, cleared.tencBox) : null;
    this.crypto.set(kind, { scheme, tenc });
    if (scheme && !this.keys) {
      this.log(`!! ${kind}: this track is ${scheme}-encrypted but NO content key was supplied — ` +
               'paste the key from WidevineProxy2 (it will play garbage until then)');
    }
    this.log(`${kind}: scheme=${scheme || 'none'} pattern=${tenc && tenc.pattern ? tenc.pattern.cryptByteBlock + ':' + tenc.pattern.skipByteBlock : 'none'} ivSize=${tenc ? tenc.perSampleIvSize : '-'}`);
  }

  // --- feeding ------------------------------------------------------------

  async feed() {
    let n = 0;
    const total = this.tracks.reduce((a, t) => a + t.segments.length, 0);
    for (const track of this.tracks) {
      if (track.kind === 'multiplexed') {
        for (const seg of track.segments) {
          const bytes = seg.bytes || await this.proxyFetch(seg.url, this.headers);
          this.markAppended(track.kind, seg.url, seg.start, seg.duration);
          for (const kind of track.splitKinds) {
            const demuxed = demuxSegment(bytes, kind, track.trackMap);
            if (!demuxed) continue;
            const clear = await this.decryptFor(kind, demuxed);
            await this.player.append(kind, clear);
            const t = fragmentTimes(clear);
            if (t.length) this.nextTime.set(kind, t[t.length - 1].end);
            await this.emitDump(`seg-${kind}-${String(n).padStart(4, '0')}.mp4`, clear);
          }
          n++;
          if (n % 5 === 0) this.log(`processed ${n}/${total} segments`);
        }
        continue;
      }

      for (const seg of track.segments) {
        let bytes = seg.bytes || await this.proxyFetch(seg.url, this.headers);
        this.markAppended(track.kind, seg.url, seg.start, seg.duration);
        if (seg.key && seg.key.method === 'AES-128') {
          bytes = await this.decryptHlsAes128(bytes, seg);
        } else {
          bytes = await this.decryptFor(track.kind, bytes);
        }
        await this.player.append(track.kind, bytes);
        // Remember where the timeline continues, so the live refresh can
        // pick up exactly there.
        const times = fragmentTimes(bytes);
        if (times.length) this.nextTime.set(track.kind, times[times.length - 1].end);
        await this.emitDump(`seg-${track.kind}-${String(n).padStart(4, '0')}.mp4`, bytes);
        n++;
        if (n % 5 === 0) this.log(`processed ${n}/${total} segments`);
      }
    }
    this.log(`done: ${n} segments`);
    if (this.isLive === false) {
      await this.player.endOfStream();
    } else {
      this.startLiveRefresh();
    }
    return n;
  }

  // Stop the background timers. Without this a second Load leaves the old
  // pipeline appending to a detached SourceBuffer.
  stop() {
    this.stopped = true;
    if (this.liveTimer) { clearInterval(this.liveTimer); this.liveTimer = null; }
    if (this.nudgeTimer) { clearInterval(this.nudgeTimer); this.nudgeTimer = null; }
    this.refreshing = false;
  }

  markAppended(kind, url, start, duration) {
    if (!url) return;
    if (!this.appended.has(kind)) this.appended.set(kind, new Set());
    this.appended.get(kind).add(url);
    if (typeof start === 'number') this.lastStart.set(kind, start);
    if (typeof duration === 'number') {
      if (!this.lastDuration) this.lastDuration = new Map();
      this.lastDuration.set(kind, duration);
    }
  }

  // A live manifest keeps moving: re-read it and feed whatever is new,
  // otherwise the element runs out of buffer and pauses after the first
  // window of segments.
  startLiveRefresh() {
    if (this.liveTimer) return;
    this.log(`live: refreshing the manifest every ${this.liveRefreshMs} ms`);
    this.liveTimer = setInterval(() => {
      // The user can switch the follow off at any time; the timer keeps
      // running but does nothing, so switching it back on takes effect
      // immediately.
      if (!this.followLive) return;
      this.refreshLive().catch((e) => this.log(`live refresh failed: ${e.message}`));
    }, this.liveRefreshMs);
    // Nudge the element out of a stall (gap between windows) promptly.
    this.nudgeTimer = setInterval(() => {
      this.player.ensurePlaybackPosition();
    }, 2000);
  }

  async refreshLive() {
    if (!this.manifestUrl) return;
    if (this.refreshing) return;   // single flight: never overlap refreshes
    this.refreshing = true;
    try {
      await this.refreshLiveOnce();
    } finally {
      this.refreshing = false;
    }
  }

  // CDNs cache manifests aggressively; without a cache-buster the same
  // (stale) manifest comes back and the window never moves.
  freshManifestUrl() {
    try {
      const u = new URL(this.manifestUrl);
      u.searchParams.set('_', String(Date.now()));
      return u.href;
    } catch {
      return this.manifestUrl;
    }
  }

  async refreshLiveOnce() {
    // A refresh that was already in flight when the pipeline was stopped
    // must not touch the SourceBuffers any more: the new run has replaced
    // the MediaSource, and appending to it would fail.
    if (this.stopped) return;
    const source = await this.resolveSource(this.freshManifestUrl());
    let added = 0;
    for (const track of source.tracks) {
      if (this.stopped) return;
      if (!track.kind || track.kind === 'multiplexed') continue;
      const seen = this.appended.get(track.kind) || new Set();
      // The new manifest describes its window with a timeline that restarts
      // at 0, so the fragments have to be re-stamped to continue where the
      // previous window ended.
      // The manifest only advertises the last window, but the segment URLs
      // are templated: the segments after it are reachable by number. Their
      // fragments carry absolute timestamps already, so they are appended
      // unchanged - re-stamping them is what breaks the decoder.
      // The manifest's window slides forward; the fragments inside carry
      // absolute timestamps, so a segment is appended when it continues (or
      // just follows) the timeline we have already fed.
      let expected = this.nextTime.get(track.kind);
      let gap = 0;
      for (const seg of track.segments) {
        if (seen.has(seg.url)) continue;
        if (expected === undefined) break;
        let bytes;
        try {
          bytes = await this.proxyFetch(seg.url, this.headers);
        } catch (e) {
          continue; // not published (yet)
        }
        const times = fragmentTimes(bytes);
        if (!times.length) continue;
        if (times[0].start < expected) continue;   // older than what we have
        if (times[0].start > expected + 30 * (track.timescale || 1)) continue; // a hole too big
        if (times[0].start > expected) gap += times[0].start - expected;
        seen.add(seg.url);
        this.markAppended(track.kind, seg.url, seg.start, seg.duration);
        const clear = await this.decryptFor(track.kind, bytes);
        await this.player.append(track.kind, clear);
        expected = times[times.length - 1].end;
        added++;
      }
      if (expected !== undefined) this.nextTime.set(track.kind, expected);
      if (added) this.log(`live: ${track.kind} +${added} segment(s)${gap ? `, skipped ${gap / (track.timescale || 1)}s` : ''}`);
    }

  }

  async decryptFor(kind, bytes) {
    const c = this.crypto.get(kind);
    if (!c || !c.scheme || !this.keys) return bytes;
    const r = await decryptSegment(bytes, c.tenc, c.scheme, this.keys);
    if (r.samples === 0 && r.sencFound === false) {
      this.log(`WARN: no senc box found in a ${kind} segment — nothing was decrypted`);
    }
    return r.buffer;
  }

  async decryptHlsAes128(bytes, seg) {
    const uri = seg.key.uri ? new URL(seg.key.uri, seg.url).href : seg.url;
    let key = this.hlsKeys.get(uri);
    if (!key) {
      const raw = await this.proxyFetch(uri, this.headers);
      key = raw.slice(0, 16);
      this.hlsKeys.set(uri, key);
    }
    let iv;
    if (seg.key.iv) {
      iv = new Uint8Array(16);
      const b = hexToBytes(seg.key.iv.replace(/^0x/i, ''));
      iv.set(b.subarray(Math.max(0, b.length - 16)), Math.max(0, 16 - b.length));
    } else {
      iv = new Uint8Array(16);
      new DataView(iv.buffer).setUint32(12, seg.sequence >>> 0);
    }
    return await decryptAes128(bytes, key, iv);
  }

  async emitDump(name, bytes) {
    if (!this.dump) return;
    await this.dump(name, bytes);
  }
}

// Trailing number of a segment URL (".../000197260.m4s" -> 197260).
function segmentNumber(url) {
  const m = String(url || '').match(/(\d+)(?:\.[A-Za-z0-9]+)?$/);
  return m ? Number(m[1]) : null;
}

function hexToBytes(s) {
  const out = new Uint8Array(s.length >> 1);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(s.substr(i * 2, 2), 16);
  return out;
}
