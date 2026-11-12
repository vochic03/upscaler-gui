// player.js — clear-MSE playback + capture.
//
// The whole point: the <video> element is fed *decrypted* segments through
// Media Source Extensions and never has a MediaKeys object attached, so
//   HTMLMediaElement.captureStream()  -> allowed
//   IsMediaDataCorsSameOrigin()        -> true (segments fetched with CORS)
//
// In Chromium the captureStream() guard is
//   blink::HTMLMediaElementEncryptedMedia::mediaKeys(element)
// i.e. "did this element ever run EME", not "is the data encrypted".

import { makeClearInit, deriveCodecs } from './boxes.js';

// How much video to keep buffered. At 1080p this is roughly 80 MB, well
// under the point where Chromium starts refusing appends.
const MAX_BUFFER_S = 120;
// How much to keep behind the playhead, so a small seek backwards is instant.
const KEEP_BEHIND_S = 10;

// MSE needs an explicit codecs parameter. Derive it from the init segment,
// then fall back to the common ones.
function pickMime(kind, initBytes) {
  const codecs = deriveCodecs(initBytes);
  const list = [];
  if (kind === 'video') {
    if (codecs.video) list.push(`video/mp4; codecs="${codecs.video}"`);
    list.push(
      'video/mp4; codecs="avc1.42E01E"',
      'video/mp4; codecs="avc1.4d401f"',
      'video/mp4; codecs="avc1.640028"',
      'video/mp4; codecs="hvc1.1.6.L93.B0"',
    );
  } else {
    if (codecs.audio) list.push(`audio/mp4; codecs="${codecs.audio}"`);
    list.push(
      'audio/mp4; codecs="mp4a.40.2"',
      'audio/mp4; codecs="mp4a.40.5"',
      'audio/mp4; codecs="opus"',
      'audio/mp4; codecs="ac-3"',
    );
  }
  const hit = list.find((t) => {
    try { return MediaSource.isTypeSupported(t); } catch { return false; }
  });
  if (!hit) throw new Error(`no supported ${kind}/mp4 mime type (tried ${list.join(', ')})`);
  return hit;
}

// The WebAudio graph belongs to the *element*, not to the player.
//
// An element can only be connected to a MediaElementAudioSourceNode once:
// a second call throws "HTMLMediaElement already connected previously to a
// different MediaElementAudioSourceNode". Loading a second stream creates a
// new player for the same <video>, so the graph has to be cached on the
// element and reused - otherwise every recording after the first loses its
// audio track.
function audioGraphFor(video, log) {
  if (video.__captureGraph) return video.__captureGraph;
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  // The context starts suspended unless we are in a user gesture.
  if (ctx.state === 'suspended') ctx.resume().catch(() => {});
  const dest = ctx.createMediaStreamDestination();
  const source = ctx.createMediaElementSource(video);
  source.connect(dest);
  source.connect(ctx.destination);
  // A meter, so it is visible whether the recording carries sound.
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 2048;
  source.connect(analyser);
  const graph = { ctx, dest, source, analyser };
  video.__captureGraph = graph;
  if (log) log(`audio graph created (context ${ctx.state})`);
  return graph;
}

export class ClearPlayer {
  constructor(videoEl, log = () => {}) {
    this.video = videoEl;
    this.log = log;
    this.mediaSource = null;
    this.sourceBuffers = new Map(); // 'video' | 'audio' -> SourceBuffer
    this.pendingInits = new Map();   // kind -> init segment bytes
    this.queue = Promise.resolve();
    this.recorder = null;
    this.chunks = [];
    this.audioCtx = null;
    this.audioDest = null;
    this.ready = false;
  }

  // Create the MediaSource. One per element; tracks are added afterwards.
  //
  // 'sourceopen' does not always arrive (the element can abort the resource
  // selection when it is reset), so this retries instead of hanging forever.
  async open() {
    for (let attempt = 0; attempt < 4; attempt++) {
      const ms = new MediaSource();
      this.mediaSource = ms;

      // Attach the listener *before* assigning the src, and resolve
      // immediately if it is already open.
      const opened = new Promise((resolve) => {
        if (ms.readyState === 'open') { resolve(); return; }
        ms.addEventListener('sourceopen', resolve, { once: true });
      });
      this.video.src = URL.createObjectURL(ms);
      this.video.load();

      const ok = await Promise.race([
        opened.then(() => true),
        new Promise((r) => setTimeout(() => r(false), 3000)),
      ]);
      if (ok) {
        this.ready = true;
        return;
      }
      this.log(`media source did not open (attempt ${attempt + 1}) — retrying`);
      // Chrome does not open a MediaSource in a hidden tab, so wait for the
      // tab to come back to the foreground instead of giving up.
      if (document.hidden) {
        this.log('waiting for this tab to become visible');
        await this.waitForVisible(120000);
      }
    }
    throw new Error(document.hidden
      ? 'the media source would not open: this tab is in the background (Chrome throttles media in hidden tabs)'
      : 'the media source would not open');
  }

  // Add one media track (video or audio) with its own init segment.
  //
  // The SourceBuffer is created here but the init segment is only appended
  // later, by commitInits(). Chromium caps the number of SourceBuffers at
  // the number of tracks in the *first* init segment it receives, so all of
  // them have to exist before any init is appended.
  async addTrack(kind, initBytes) {
    const mime = pickMime(kind, initBytes);
    const sb = this.mediaSource.addSourceBuffer(mime);
    sb.mode = 'segments';
    sb.addEventListener('error', () => this.log(`ERROR: ${kind} source buffer error`));
    this.sourceBuffers.set(kind, sb);
    this.pendingInits.set(kind, initBytes);
    this.log(`track ${kind} registered (${mime})`);
  }

  // Append every init segment that was registered.
  async commitInits() {
    for (const [kind, bytes] of this.pendingInits) {
      const sb = this.sourceBuffers.get(kind);
      if (!sb) continue;
      await this.appendToBuffer(kind, sb, bytes);
      this.log(`track ${kind} init appended`);
    }
    this.pendingInits.clear();
  }

  append(kind, bytes) {
    this.queue = this.queue
      .then(() => {
        const sb = this.sourceBuffers.get(kind);
        if (!sb) return;
        return this.appendToBuffer(kind, sb, bytes);
      })
      .catch((e) => this.log(`append(${kind}) failed: ${e.message}`));
    return this.queue;
  }

  async appendToBuffer(kind, sb, bytes) {
    if (sb.updating) {
      await new Promise((r) => sb.addEventListener('updateend', r, { once: true }));
    }
    await this.evict(sb);
    try {
      sb.appendBuffer(bytes);
    } catch (e) {
      this.log(`append to ${kind} rejected: ${e.message}`);
      return;
    }
    await new Promise((r) => sb.addEventListener('updateend', r, { once: true }));
    this.ensurePlaybackPosition();
  }

  // Live streams start at a large media timestamp (the catch-up position),
  // so the element sits at 0 and never starts on its own. Nudge it into the
  // buffered range as soon as there is one.
  ensurePlaybackPosition() {
    const v = this.video;
    // Recover from a stall:
    //  * the element sits at the end of the buffer  -> nudge to the live edge
    //  * the element sits in a gap between two ranges -> jump across it
    if (!v.buffered || !v.buffered.length) return;
    const t = v.currentTime;
    for (let i = 0; i < v.buffered.length; i++) {
      const s = v.buffered.start(i);
      const e = v.buffered.end(i);
      if (t >= s - 0.5 && t <= e + 0.5) {
        if (v.readyState < 3 && e - t < 0.5) {
          try { v.currentTime = Math.max(s, e - 0.25); } catch { /* ignore */ }
        }
        return;
      }
      if (t < s) {
        // We are in a hole before this range: jump to it.
        try { v.currentTime = s + 0.05; } catch { /* ignore */ }
        return;
      }
    }
    const end = v.buffered.end(v.buffered.length - 1);
    if (t > end - 0.5) {
      try { v.currentTime = Math.max(v.buffered.start(0), end - 0.25); } catch { /* ignore */ }
    }
    try {
      if (v.currentTime > 0.001) return;
      if (!v.buffered || !v.buffered.length) return;
      const start = v.buffered.start(0);
      if (start > 0) v.currentTime = start + 0.1;
    } catch { /* not seekable yet */ }
  }

  // Keep the SourceBuffer bounded.
  //
  // Chromium rejects appendBuffer() once the SourceBuffer is full, and the
  // buffer only drains if the playhead moves. A paused element therefore
  // fills up after a few minutes of live refresh, every further segment is
  // rejected, a hole opens in the timeline and the player sits there
  // "buffering" forever.
  //
  // Two rules, and the second one matters:
  //   * normally, drop what is behind the playhead - the rest stays
  //     contiguous;
  //   * never drop what is *ahead* of it. Removing a range in the middle
  //     leaves a hole, and the next segment is appended on the far side of
  //     that hole, so playback stops dead at it.
  //     So when the playhead is stuck and the buffer would overflow, the
  //     whole buffer is dropped instead and the next segment starts a fresh,
  //     contiguous range at the live edge.
  //
  // The removal must be awaited: an appendBuffer() while a remove() is still
  // running is rejected with "This SourceBuffer is still processing an
  // 'appendBuffer' or 'remove'".
  async evict(sb) {
    try {
      const t = this.video.currentTime;
      // How much is buffered, across every track.
      let lo = Infinity;
      let hi = -Infinity;
      for (const b of this.sourceBuffers.values()) {
        if (!b.buffered.length) continue;
        lo = Math.min(lo, b.buffered.start(0));
        hi = Math.max(hi, b.buffered.end(b.buffered.length - 1));
      }
      const span = hi > lo ? hi - lo : 0;
      if (span > MAX_BUFFER_S) {
        await this.resetBuffers();
        return;
      }
      if (!sb.buffered.length) return;
      const s = sb.buffered.start(0);
      if (t - s > KEEP_BEHIND_S) {
        sb.remove(s, t - KEEP_BEHIND_S);
        if (sb.updating) {
          await new Promise((r) => sb.addEventListener('updateend', r, { once: true }));
        }
      }
    } catch { /* nothing buffered yet */ }
  }

  // Drop the whole buffer of every track, so the next segment starts a
  // fresh, contiguous range.
  async resetBuffers() {
    for (const sb of this.sourceBuffers.values()) {
      try {
        if (!sb.buffered.length) continue;
        sb.remove(0, Infinity);
        if (sb.updating) {
          await new Promise((r) => sb.addEventListener('updateend', r, { once: true }));
        }
      } catch { /* ignore */ }
    }
  }

  // --- capture ------------------------------------------------------------

  captureStream() {
    const vStream = this.video.captureStream();
    const tracks = [...vStream.getVideoTracks()];

    // Chrome's captureStream() on a <video> yields video only; route the
    // element's audio through WebAudio to get an audio track.
    try {
      const graph = audioGraphFor(this.video, this.log);
      this.audioCtx = graph.ctx;
      this.audioDest = graph.dest;
      this.sourceNode = graph.source;
      this.analyser = graph.analyser;
      const at = graph.dest.stream.getAudioTracks();
      if (at.length) tracks.push(at[0]);
      else this.log('audio capture unavailable: the destination has no audio track');
    } catch (e) {
      this.log('audio capture unavailable: ' + e.message);
    }
    return new MediaStream(tracks);
  }

  // Resolves when the document is visible again (or after the timeout).
  waitForVisible(timeout = 30000) {
    return new Promise((resolve) => {
      if (!document.hidden) { resolve(); return; }
      const done = () => {
        clearTimeout(timer);
        document.removeEventListener('visibilitychange', onVis);
        resolve();
      };
      const onVis = () => { if (!document.hidden) done(); };
      const timer = setTimeout(done, timeout);
      document.addEventListener('visibilitychange', onVis);
    });
  }

  // Signal level (0..1) of the audio the recording would capture.
  //
  // The peak across the spectrum is used rather than the RMS: speech has
  // silent gaps, and an RMS meter flickers to "silent" between syllables.
  audioLevel() {
    if (!this.analyser) return null;
    const bins = new Float32Array(this.analyser.frequencyBinCount);
    this.analyser.getFloatFrequencyData(bins);
    let peak = 0;
    for (const db of bins) {
      const mag = Math.pow(10, db / 20);
      if (mag > peak) peak = mag;
    }
    return peak;
  }

  startRecording(mimeType) {
    if (this.recorder && this.recorder.state === 'recording') return this.recorder;
    const types = mimeType ? [mimeType] : [
      'video/webm;codecs=vp9,opus',
      'video/webm;codecs=vp8,opus',
      'video/webm',
    ];
    let chosen = null;
    for (const t of types) {
      if (window.MediaRecorder && MediaRecorder.isTypeSupported(t)) { chosen = t; break; }
    }
    if (!chosen) throw new Error('MediaRecorder has no supported webm type');

    const stream = this.captureStream();
    this.lastStream = stream;
    this.log('recording tracks: ' + stream.getTracks().map((t) => t.kind).join(', '));
    this.chunks = [];
    const rec = new MediaRecorder(stream, { mimeType: chosen });
    rec.ondataavailable = (e) => { if (e.data && e.data.size) this.chunks.push(e.data); };
    rec.start(1000);
    this.recorder = rec;
    this.recordMime = chosen;
    this.log('recording started (' + chosen + ')');
    return rec;
  }

  stopRecording() {
    return new Promise((resolve) => {
      if (!this.recorder || this.recorder.state === 'inactive') { resolve(null); return; }
      const rec = this.recorder;
      rec.onstop = () => {
        const blob = new Blob(this.chunks, { type: this.recordMime || 'video/webm' });
        this.recorder = null;
        this.chunks = [];
        this.log(`recording stopped: ${(blob.size / 1024 / 1024).toFixed(2)} MiB`);
        resolve(blob);
      };
      rec.stop();
    });
  }

  // Signal that no more segments will be appended (VOD only). Without this
  // the media element reports an infinite duration.
  async endOfStream() {
    if (!this.mediaSource || this.mediaSource.readyState !== 'open') return;
    try {
      this.mediaSource.endOfStream();
    } catch (e) {
      this.log('endOfStream failed: ' + e.message);
    }
  }

  get recording() {
    return !!(this.recorder && this.recorder.state === 'recording');
  }
}
