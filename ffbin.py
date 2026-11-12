"""Which ffmpeg to use.

Homebrew replaces ffmpeg and its libraries one at a time, and in the
middle of that swap the binary on the PATH cannot load its own libraries
any more. A job that joins its pieces at that exact moment dies with a
loader error about libvpx - which is what happened on 2026-11-02, in the
middle of a cloud upscale that had already paid for the card.

So: find an ffmpeg that actually RUNS, and keep it. A binary that cannot
print its version cannot encode anything either.
"""
import os
import shutil
import subprocess

# The usual places, in the order they are tried. The one on the PATH is
# the last resort, so a broken link does not decide it.
CANDIDATES = ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg",
               "/usr/bin/ffmpeg"]

_chosen = None


def ffmpeg():
    """A working ffmpeg, or the name on the PATH."""
    global _chosen
    if _chosen:
        return _chosen
    for cand in CANDIDATES:
        if not (os.path.isfile(cand) and os.access(cand, os.X_OK)):
            continue
        try:
            r = subprocess.run([cand, "-version"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=20)
            if r.returncode == 0:
                _chosen = cand
                return cand
        except Exception:
            continue
    _chosen = shutil.which("ffmpeg") or "ffmpeg"
    return _chosen


def reset():
    """Look again - the next call after a Homebrew upgrade."""
    global _chosen
    _chosen = None
