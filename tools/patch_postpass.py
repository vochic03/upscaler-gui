#!/usr/bin/env python3
"""Move the grain and the halation into a second pass over the finished file.

Both need a second filter input, and a second input inside the piped encoder
stalls the framesync. The picture's frames arrive at the encoder in bursts -
the AI pass writes a chunk, pauses while the next chunk is upscaled, and so
on - so the picture's timestamps jump. The framesync answers a gap by pulling
the other input, and with an endless or looping second input it pulls forever
while the picture is still arriving. The job never leaves "finishing".

Over a finished file both inputs are files, the timestamps are dense, and the
same graph runs in about 80 ms a frame. So the encoder does the picture and
the post pass does the grain and the halation.
"""
import sys

PATH = "/Users/vasilijemicic/upscaler-gui/engine.py"
src = open(PATH, encoding="utf-8").read()
ok = True


def sub(old, new, label):
    global src, ok
    if old not in src:
        print("MISS:", label)
        ok = False
        return
    if src.count(old) != 1:
        print("AMBIGUOUS (%d):" % src.count(old), label)
        ok = False
        return
    src = src.replace(old, new)
    print("ok:", label)


# 1. the encoder keeps only what it can do well -----------------------
sub('''        if look.get("grain_mode") == "perlin" and int(look.get("grain", 0)) > 0:
            # The grain source. An endless lavfi stream as a second input
            # makes the filtergraph wait for it forever, so the pattern is
            # rendered once into a small file and looped instead: three
            # seconds of texture that drifts and then repeats.
            #
            # random_mode=seed with a fixed seed keeps the same texture from
            # run to run.
            gw, gh = max(2, (target[0] if target else ai_w) // 4), \\
                     max(2, (target[1] if target else ai_h) // 4)
            grain_file = os.path.join(self.work, "grain.gray")
            if not os.path.exists(grain_file):
                gen = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                       f"perlin=size={gw}x{gh}:rate={fps:.6f}:"
                       f"tscale={look.get('grain_tscale') or 0.08}:octaves=2:"
                       f"persistence=0.5:random_mode=seed:random_seed=1",
                       "-t", "3", "-pix_fmt", "gray", "-f", "rawvideo",
                       grain_file]
                subprocess.run(gen, check=True)
            ff += ["-f", "rawvideo", "-pixel_format", "gray",
                   "-video_size", f"{gw}x{gh}", "-framerate", f"{fps:.6f}",
                   "-stream_loop", "-1", "-i", grain_file]
''', '', "the grain input leaves the encoder")

sub('''            node = "[v0]"
            fw, fh = target if target else (ai_w, ai_h)
            if halation:
                # A blurred copy of the luma, screened back at a few per
                # cent: the soft halo around highlights that a film print has
                # and digital never has. The copy is luma only, so the colour
                # stays where it is.
                parts.append(f"{node}split=2[base][hlsrc]")
                parts.append(f"[hlsrc]format=gray,gblur=sigma={halation_sigma},"
                             f"format=yuv420p[hlb]")
                parts.append(f"[base][hlb]blend=c0_expr='A+(B-128)*{halation}':"
                             f"c1_expr='A':c2_expr='A'[v1]")
                node = "[v1]"
            if perlin_grain:
                # Film grain: temporally correlated, so it drifts instead of
                # sitting still. Generated at half the size and scaled up,
                # which is cheaper and more like real grain.
                parts.append(f"[2:v]scale={fw}:{fh}:flags=bilinear,"
                             f"format=yuv420p[bn]")
                parts.append(f"{node}[bn]blend=c0_expr='A+(B-128)*{grain_s}':"
                             f"c1_expr='A':c2_expr='A'[v2]")
                node = "[v2]"
            parts.append(node + "null[v]")''',
    '''            # The grain and the halation are not here: they need a second
            # input, and a second input inside this piped encoder stalls the
            # framesync. They run as a post pass over the finished file.
            parts.append("[v0]null[v]")''',
    "the encoder chain goes back to one branch")

# 2. the post pass -----------------------------------------------------
sub('''        if os.path.exists(self.out):
            self.say(f"output: {os.path.getsize(self.out)/1e6:.1f} MB")
        else:
            raise RuntimeError("ffmpeg produced no output file")''',
    '''        if not os.path.exists(self.out):
            raise RuntimeError("ffmpeg produced no output file")

        # --- 6. grain and halation, as a post pass -------------------------
        look = LOOKS.get(self.opts.get("look") or "clean") or LOOKS["clean"]
        grain = int(look.get("grain") or 0)
        halation = float(look.get("halation") or 0)
        perlin = look.get("grain_mode") == "perlin" and grain > 0
        if grain or halation:
            try:
                self._post_pass(look, grain, halation, perlin)
            except Exception as e:
                # A look that fails to finish should not throw away the
                # upscale that already worked.
                self.say(f"the post pass did not run: {e}")

        if os.path.exists(self.out):
            self.say(f"output: {os.path.getsize(self.out)/1e6:.1f} MB")''',
    "the post pass is called")

# 3. the post pass itself ----------------------------------------------
sub('''def default_model_for(width, height):''',
    '''def _post_pass(self, look, grain, halation, perlin):
    """Grain and halation, over the finished file.

    The picture is already encoded at this point, so both inputs are files
    and the filtergraph is quick. The grain is perlin when the look asks for
    it - a noise field with continuity in space and time, so it drifts the
    way emulsion does instead of sitting still like dirt on a lens.
    """
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
         self.out], stdout=subprocess.PIPE).stdout.decode())
    v = next(s for s in info["streams"] if s.get("codec_type") == "video")
    w, h = int(v["width"]), int(v["height"])
    try:
        num, den = (v.get("avg_frame_rate") or "25/1").split("/")
        fps = float(num) / float(den) if float(den) else 25.0
    except Exception:
        fps = 25.0

    inputs = []
    chain = []
    node = "[0:v]"
    if halation:
        # A blurred copy of the luma, screened back at a few per cent: the
        # soft halo around highlights that a film print has and digital never
        # has. The copy is luma only, so the colour stays where it is.
        sigma = max(4, min(24, h // 90))
        chain.append("[0:v]split=2[base][hlsrc]")
        chain.append(f"[hlsrc]format=gray,gblur=sigma={sigma},format=yuv420p[hlb]")
        chain.append(f"[base][hlb]blend=c0_expr='A+(B-128)*{halation}':"
                     f"c1_expr='A':c2_expr='A'[v1]")
        node = "[v1]"
    if grain:
        if perlin:
            # The pattern is rendered once into a small file and looped:
            # three seconds of texture that drifts and then repeats.
            gw, gh = max(2, w // 4), max(2, h // 4)
            grain_file = os.path.join(self.work, "grain.gray")
            if not os.path.exists(grain_file):
                subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                     f"perlin=size={gw}x{gh}:rate={fps:.6f}:"
                     f"tscale={look.get('grain_tscale') or 0.08}:octaves=2:"
                     f"persistence=0.5:random_mode=seed:random_seed=1",
                     "-t", "3", "-pix_fmt", "gray", "-f", "rawvideo",
                     grain_file], check=True)
            inputs = ["-f", "rawvideo", "-pixel_format", "gray",
                      "-video_size", f"{gw}x{gh}", "-framerate", f"{fps:.6f}",
                      "-stream_loop", "-1", "-i", grain_file]
            chain.append("[1:v]scale=%d:%d:flags=bilinear,format=yuv420p[bn]" % (w, h))
            strength = grain * 0.05
        else:
            # The frozen pattern that ffmpeg's own noise filter leaves
            # behind. Kept as the default because it is instant.
            chain.append(f"{node}noise=alls={grain}:allf=u[v1]")
            strength = 0.0
        if perlin:
            chain.append(f"{node}[bn]blend=c0_expr='A+(B-128)*{strength}':"
                         f"c1_expr='A':c2_expr='A'[v2]")
            node = "[v2]"

    tmp = os.path.join(self.work, "post.mp4")
    enc = ENCODERS.get(self.opts.get("encoder") or "h264", ENCODERS["h264"])[1]
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", self.out] + inputs + \\
          ["-filter_complex", ";".join(chain), "-map", node, "-map", "0:a?",
           "-c:a", "copy"] + list(enc) + [tmp]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0 or not os.path.exists(tmp):
        raise RuntimeError(res.stderr.decode("utf-8", "replace")[-400:])
    shutil.move(tmp, self.out)
    self.say("grain and halation applied")


def default_model_for(width, height):''',
    "the post pass function")

if not ok:
    print("SOME EDITS MISSED")
    sys.exit(1)
open(PATH, "w", encoding="utf-8").write(src)
print("engine.py patched")
