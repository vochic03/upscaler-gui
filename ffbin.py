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


_filters = {}


def has_filter(name):
    """Does the chosen ffmpeg have this filter?

    Some of the finishing needs a recent ffmpeg: the `perlin` grain source
    arrived in 7.1, and the ffmpeg that Ubuntu 24.04 and Debian ship is
    6.1. Without this check the finishing pass died at the very end - after
    the card had done the work and been paid for - opening a grain file
    that the generator had silently failed to make.
    """
    binary = ffmpeg()
    key = (binary, name)
    if key not in _filters:
        try:
            out = subprocess.run([binary, "-hide_banner", "-filters"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 timeout=20).stdout.decode("utf-8", "replace")
            _filters[key] = any(line.split()[1:2] == [name]
                                for line in out.splitlines() if line.strip())
        except Exception:
            _filters[key] = False
    return _filters[key]
