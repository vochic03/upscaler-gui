#!/usr/bin/env python3
"""
Build the local test fixtures and the ground truth used to validate the
in-browser CENC decryption.

Fixtures are produced with shaka-packager (the industry-standard muxer), so
the init segments carry a real pssh/schm/sinf/tenc structure and every media
segment carries a correct per-sample senc.

  dash-ctr/  DASH, separate video + audio representations, AES-CTR
  dash-cbc/  DASH, separate video + audio representations, AES-CBC 1:9 pattern
  hls-ctr/   HLS-style playlist, video + audio multiplexed in one segment
  hls-cbc/   same, cbcs

Ground truth (ffmpeg's own demuxer decrypting the same files):
  gt/<name>-video.h264 and gt/<name>-audio.aac

    python3 test/make_fixture.py
"""

import os
import shutil
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures")
GT = os.path.join(FIX, "gt")
PACKAGER = os.path.join(ROOT, "tools", "packager")

KEY = "000102030405060708090a0b0c0d0e0f"
KID = "101112131415161718191a1b1c1d1e1f"
VIDEO_SRC = "testsrc2=size=320x240:rate=25:duration=8"
AUDIO_SRC = "sine=frequency=440:duration=8"


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- ISO BMFF helpers -----------------------------------------------------

def boxes(buf, start=0, end=None):
    end = len(buf) if end is None else end
    p = start
    while p + 8 <= end:
        size = struct.unpack(">I", buf[p:p + 4])[0]
        typ = buf[p + 4:p + 8].decode("latin1")
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", buf[p + 8:p + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - p
        if size < hdr or p + size > end:
            break
        yield typ, p, size, hdr
        p += size


def build_box(typ, payload):
    out = bytearray(8 + len(payload))
    struct.pack_into(">I", out, 0, 8 + len(payload))
    out[4:8] = typ.encode("latin1")
    out[8:] = payload
    return bytes(out)


def parse_trun(buf, box):
    d = box[1] + box[3]
    flags = struct.unpack(">I", buf[d:d + 4])[0] & 0xFFFFFF
    count = struct.unpack(">I", buf[d + 4:d + 8])[0]
    p = d + 8
    data_offset = 0
    if flags & 0x1:
        data_offset = struct.unpack(">i", buf[p:p + 4])[0]
        p += 4
    if flags & 0x4:
        p += 4
    sizes = []
    for _ in range(count):
        if flags & 0x100:
            p += 4
        if flags & 0x200:
            sizes.append(struct.unpack(">I", buf[p:p + 4])[0])
            p += 4
        else:
            sizes.append(None)
        if flags & 0x400:
            p += 4
        if flags & 0x800:
            p += 4
    return flags, data_offset, sizes


def parse_tfhd(buf, box):
    d = box[1] + box[3]
    flags = struct.unpack(">I", buf[d:d + 4])[0] & 0xFFFFFF
    p = d + 8
    default_size = 0
    if flags & 0x1:
        p += 8
    if flags & 0x2:
        p += 4
    if flags & 0x8:
        p += 4
    if flags & 0x10:
        default_size = struct.unpack(">I", buf[p:p + 4])[0]
        p += 4
    if flags & 0x20:
        p += 4
    return flags, default_size


# --- multiplex two single-track segment sets ------------------------------

def merge_segments(video_seg, audio_seg):
    """Merge shaka's video and audio segments into one multiplexed segment.

    The result mirrors what an HLS muxer produces: one moof holding both
    trafs, one mdat holding both tracks' sample data.
    """
    def split(seg):
        top = list(boxes(seg))
        moof = [b for b in top if b[0] == "moof"][0]
        mdat = [b for b in top if b[0] == "mdat"][0]
        kids = list(boxes(seg, moof[1] + moof[3], moof[1] + moof[2]))
        mfhd = [b for b in kids if b[0] == "mfhd"][0]
        traf = [b for b in kids if b[0] == "traf"][0]
        tkids = list(boxes(seg, traf[1] + traf[3], traf[1] + traf[2]))
        tfhd = [b for b in tkids if b[0] == "tfhd"][0]
        _, defsize = parse_tfhd(seg, tfhd)
        truns = [b for b in tkids if b[0] == "trun"]
        chunks = []
        for tr in truns:
            _, _, sizes = parse_trun(seg, tr)
            total = sum(s if s is not None else defsize for s in sizes)
            chunks.append((tr, total))
        data = seg[mdat[1] + mdat[3]: mdat[1] + mdat[2]]
        return mfhd, tkids, chunks, data

    VIDEO_ID, AUDIO_ID = 1, 2
    vmfhd, vkids, vchunks, vdata = split(video_seg)
    amfhd, akids, achunks, adata = split(audio_seg)

    def traf_bytes(seg, kids, chunks, track_offset, moof_size, new_track_id=None):
        """Rebuild one traf with data offsets pointing into the new mdat."""
        out = []
        ci = 0
        cursor = track_offset
        for k in kids:
            if k[0] in ("saiz", "saio"):
                continue  # senc is authoritative; saiz/saio would need re-offsetting
            if k[0] == "tfhd" and new_track_id is not None:
                out.append(patch_track_id(seg, k, new_track_id))
                continue
            if k[0] == "trun":
                tr, total = chunks[ci]
                d = tr[1] + tr[3]
                flags = struct.unpack(">I", seg[d:d + 4])[0] & 0xFFFFFF
                payload = bytearray()
                payload.append(seg[d])                       # version
                payload += struct.pack(">I", (flags & ~0x1) | 0x1)[1:]  # flags
                payload += seg[d + 4:d + 8]                  # sample_count
                payload += struct.pack(">i", moof_size + 8 + cursor)  # data_offset
                if flags & 0x4:
                    payload += seg[d + 12:d + 16]             # first_sample_flags
                payload += trun_body(seg, tr)                 # per-sample fields
                out.append(build_box("trun", bytes(payload)))
                cursor += total
                ci += 1
                continue
            out.append(seg[k[1]:k[1] + k[2]])
        return build_box("traf", b"".join(out))

    def trun_body(seg, box):
        """Per-sample part of a trun (after the fixed header fields)."""
        d = box[1] + box[3]
        flags = struct.unpack(">I", seg[d:d + 4])[0] & 0xFFFFFF
        p = d + 8
        if flags & 0x1:
            p += 4
        if flags & 0x4:
            p += 4
        return seg[p:box[1] + box[2]]

    mfhd_bytes = video_seg[vmfhd[1]:vmfhd[1] + vmfhd[2]]

    # First pass: build with placeholder offsets to learn the moof size.
    probe = build_box("moof", b"".join([
        mfhd_bytes,
        traf_bytes(video_seg, vkids, vchunks, 0, 0, VIDEO_ID),
        traf_bytes(audio_seg, akids, achunks, len(vdata), 0, AUDIO_ID),
    ]))
    moof_size = len(probe)

    new_moof = build_box("moof", b"".join([
        mfhd_bytes,
        traf_bytes(video_seg, vkids, vchunks, 0, moof_size, VIDEO_ID),
        traf_bytes(audio_seg, akids, achunks, len(vdata), moof_size, AUDIO_ID),
    ]))
    new_mdat = build_box("mdat", vdata + adata)
    return new_moof + new_mdat


# --- fixture assembly -----------------------------------------------------

def make_plain(tmp):
    v = os.path.join(tmp, "video.mp4")
    a = os.path.join(tmp, "audio.mp4")
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", VIDEO_SRC,
         "-c:v", "libx264", "-preset", "ultrafast", "-g", "25",
         "-keyint_min", "25", "-sc_threshold", "0", v])
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", AUDIO_SRC,
         "-c:a", "aac", "-b:a", "64k", a])
    return v, a


def package(outdir, plain_video, plain_audio, scheme, encrypt=True):
    keys = f"label=V:key_id={KID}:key={KEY},label=A:key_id={KID}:key={KEY}"
    cmd = [
        PACKAGER,
        f"in={plain_video},stream=video,drm_label=V,"
        f"init_segment={outdir}/v-init.mp4,segment_template={outdir}/v-seg-$Number$.m4s",
        f"in={plain_audio},stream=audio,drm_label=A,"
        f"init_segment={outdir}/a-init.mp4,segment_template={outdir}/a-seg-$Number$.m4s",
    ]
    if encrypt:
        cmd += ["--enable_raw_key_encryption", f"--keys={keys}", "--clear_lead", "0"]
    cmd += [
        "--protection_scheme", scheme,
        "--generate_static_live_mpd",
        "--mpd_output", f"{outdir}/manifest.mpd",
        "--segment_duration", "1",
    ]
    run(cmd)


def patch_track_id(buf, box, new_id):
    """Rewrite the track_ID of a tkhd/trex/tfhd box."""
    typ = box[0]
    d = box[1] + box[3]
    if typ == "tkhd":
        version = buf[d]
        off = d + 20 if version == 1 else d + 12
    elif typ in ("trex", "tfhd"):
        off = d + 4
    else:
        raise ValueError(typ)
    out = bytearray(buf[box[1]:box[1] + box[2]])
    struct.pack_into(">I", out, off - box[1], new_id)
    return build_box(typ, bytes(out[8:]))


def merge_init(video_init, audio_init):
    """Build one init segment holding both tracks (what an HLS fMP4 init has)."""
    def moov_of(buf):
        for t, p, s, h in boxes(buf):
            if t == "moov":
                return p, s, h
        raise ValueError("no moov")

    VIDEO_ID, AUDIO_ID = 1, 2

    vp, vs, vh = moov_of(video_init)
    ap, as_, ah = moov_of(audio_init)
    vkids = list(boxes(video_init, vp + vh, vp + vs))
    akids = list(boxes(audio_init, ap + ah, ap + as_))

    def mvex_of(kids, buf):
        for k in kids:
            if k[0] == "mvex":
                return list(boxes(buf, k[1] + k[3], k[1] + k[2]))
        return []

    v_trex = mvex_of(vkids, video_init)
    a_trex = mvex_of(akids, audio_init)

    def patch_trak(buf, kids, new_id):
        out = b""
        for k in kids:
            if k[0] == "trak":
                # the trak's tkhd carries the track_ID
                tk = list(boxes(buf, k[1] + k[3], k[1] + k[2]))
                tkhd = [x for x in tk if x[0] == "tkhd"][0]
                fixed = patch_track_id(buf, tkhd, new_id)
                head = buf[k[1]:tkhd[1]]
                out += head + fixed + buf[tkhd[1] + tkhd[2]:k[1] + k[2]]
                continue
            out += buf[k[1]:k[1] + k[2]]
        return out

    payload = b""
    for k in vkids:
        if k[0] == "mvex":
            continue
        if k[0] == "trak":
            payload += patch_trak(video_init, [k], VIDEO_ID)
            continue
        payload += video_init[k[1]:k[1] + k[2]]
    payload += patch_trak(audio_init, [k for k in akids if k[0] == "trak"], AUDIO_ID)

    v_trex = [t for t in v_trex if t[0] == "trex"]
    a_trex = [t for t in a_trex if t[0] == "trex"]
    trex_payload = b"".join(patch_track_id(video_init, t, VIDEO_ID) for t in v_trex)
    trex_payload += b"".join(patch_track_id(audio_init, t, AUDIO_ID) for t in a_trex)
    payload += build_box("mvex", trex_payload)
    new_moov = build_box("moov", payload)

    out = b""
    for t, p, s, h in boxes(video_init):
        if t == "moov":
            out += new_moov
        else:
            out += video_init[p:p + s]
    return out


def mux_segments(outdir):
    """Merge the video/audio segments into a multiplexed variant + playlist."""
    vsegs = sorted(f for f in os.listdir(outdir) if f.startswith("v-seg-"))
    n = len(vsegs)
    init = merge_init(open(os.path.join(outdir, "v-init.mp4"), "rb").read(),
                      open(os.path.join(outdir, "a-init.mp4"), "rb").read())
    with open(os.path.join(outdir, "init.mp4"), "wb") as fh:
        fh.write(init)
    for i in range(1, n + 1):
        v = open(os.path.join(outdir, f"v-seg-{i}.m4s"), "rb").read()
        a = open(os.path.join(outdir, f"a-seg-{i}.m4s"), "rb").read()
        merged = merge_segments(v, a)
        with open(os.path.join(outdir, f"seg-{i:04d}.m4s"), "wb") as fh:
            fh.write(merged)
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:1",
             "#EXT-X-MEDIA-SEQUENCE:0", '#EXT-X-MAP:URI="init.mp4"']
    for i in range(1, n + 1):
        lines += ["#EXTINF:1.0,", f"seg-{i:04d}.m4s"]
    lines.append("#EXT-X-ENDLIST")
    with open(os.path.join(outdir, "index.m3u8"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return n


def ground_truth(init_path, segments, name, decrypt=True, want_video=True, want_audio=True):
    """Extract the elementary streams with ffmpeg (decrypting when needed)."""
    os.makedirs(GT, exist_ok=True)
    merged = os.path.join(HERE, "_gt.mp4")
    with open(merged, "wb") as fh:
        fh.write(open(init_path, "rb").read())
        for s in segments:
            fh.write(open(s, "rb").read())
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if decrypt:
        base += ["-decryption_key", KEY]
    base += ["-i", merged]
    if want_video:
        run(base + ["-map", "0:v", "-c", "copy", "-f", "h264",
                    os.path.join(GT, f"{name}-video.h264")])
    if want_audio:
        run(base + ["-map", "0:a", "-c", "copy", "-f", "adts",
                    os.path.join(GT, f"{name}-audio.aac")])
    os.remove(merged)


def main():
    if os.path.isdir(FIX):
        shutil.rmtree(FIX)
    os.makedirs(FIX, exist_ok=True)
    tmp = os.path.join(HERE, "_tmp")
    os.makedirs(tmp, exist_ok=True)
    plain_v, plain_a = make_plain(tmp)

    for scheme, tag in (("cenc", "ctr"), ("cbcs", "cbc")):
        print(f"=== {scheme} ===")
        dash = os.path.join(FIX, f"dash-{tag}")
        os.makedirs(dash, exist_ok=True)
        package(dash, plain_v, plain_a, scheme)
        vsegs = sorted(f for f in os.listdir(dash) if f.startswith("v-seg-"))
        asegs = sorted(f for f in os.listdir(dash) if f.startswith("a-seg-"))
        print(f"  dash-{tag}: {len(vsegs)} video + {len(asegs)} audio segments")

        hls = os.path.join(FIX, f"hls-{tag}")
        os.makedirs(hls, exist_ok=True)
        package(hls, plain_v, plain_a, scheme)
        n = mux_segments(hls)
        print(f"  hls-{tag}:  {n} multiplexed segments")

        # Reference: the same content packaged *without* encryption. Comparing
        # the decrypted output against this proves the fixture + ffmpeg agree.
        ref = os.path.join(tmp, f"ref-{tag}")
        os.makedirs(ref, exist_ok=True)
        package(ref, plain_v, plain_a, scheme, encrypt=False)
        rvs = sorted(f for f in os.listdir(ref) if f.startswith("v-seg-"))
        ras = sorted(f for f in os.listdir(ref) if f.startswith("a-seg-"))

        ground_truth(os.path.join(dash, "v-init.mp4"),
                     [os.path.join(dash, f) for f in vsegs], f"dash-{tag}",
                     decrypt=True, want_video=True, want_audio=False)
        ground_truth(os.path.join(dash, "a-init.mp4"),
                     [os.path.join(dash, f) for f in asegs], f"dash-{tag}",
                     decrypt=True, want_video=False, want_audio=True)
        ground_truth(os.path.join(hls, "init.mp4"),
                     [os.path.join(hls, f"seg-{i:04d}.m4s") for i in range(1, n + 1)],
                     f"hls-{tag}", decrypt=True)

        ground_truth(os.path.join(ref, "v-init.mp4"),
                     [os.path.join(ref, f) for f in rvs], f"ref-{tag}",
                     decrypt=False, want_video=True, want_audio=False)
        ground_truth(os.path.join(ref, "a-init.mp4"),
                     [os.path.join(ref, f) for f in ras], f"ref-{tag}",
                     decrypt=False, want_video=False, want_audio=True)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nfixtures in {FIX}\nground truth in {GT}\nkey={KEY} kid={KID}")


if __name__ == "__main__":
    sys.exit(main())
