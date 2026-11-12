#!/usr/bin/env python3
"""Keep the project's scratch space off the disk when the disk is full.

    python3 tools/space_guard.py            # what is going on
    python3 tools/space_guard.py relocate   # move the scratch to iCloud
    python3 tools/space_guard.py restore    # and move it back

Why iCloud Drive
----------------
The scratch space is ~/upscaler-gui/work: the uploads, the job folders and
the finished videos. A big film puts a couple of gigabytes in there, and
when the disk fills a job dies halfway through with "no space left on
device".

The iCloud Drive container is on the same volume, so putting a file there
does not create space by itself - but macOS has "Optimize Mac Storage" on
(the flag is `optimize-storage = 1` under com.apple.bird), and with it on
macOS evicts files from iCloud Drive when the disk gets tight and leaves a
placeholder behind. The files stay readable: reading one that was evicted
downloads it again.

That is the whole mechanism, and it is worth being clear about what it is
and what it is not:

  * it is a **pressure valve**, not a spare disk. The scratch still takes
    local space until macOS decides to reclaim it.
  * there is no reliable way to evict on demand. `brctl evict` refuses
    ("The file cannot be evicted", NSFileProviderErrorDomain -2008), and
    `fileproviderctl` has no evict verb at all on this macOS.
  * so the rule is: put the scratch in the container, and let macOS do the
    reclaiming when the disk is under pressure. The proof that it works is
    in the user's own iCloud Drive - files over 900 MB that read as 0 B
    locally, because they are placeholders.
  * what it costs: the scratch is uploaded to the user's iCloud, so it
    takes bandwidth and a little of their iCloud quota. It is scratch
    space, so it is thrown away afterwards - but the upload is real.
  * a file that is open while macOS evicts it keeps working: the open
    descriptor holds the data. A file that is read again afterwards is
    downloaded again, which is slow but works.

What this tool does
-------------------
`relocate` moves the work directory into the container and leaves a
symlink behind, so every path in the project keeps working unchanged.
`restore` moves it back.
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRATCH = os.path.join(ROOT, "work")

ICLOUD_CONTAINER = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs")
ICLOUD_SCRATCH = os.path.join(ICLOUD_CONTAINER, "upscaler-scratch")

# Below this much free space the scratch is moved off the disk.
FREE_WARN_GIB = 10
GIB = 1024 ** 3


def free_bytes(path="/"):
    """The free space on the volume that holds `path`."""
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    except OSError:
        return None


def dir_size(path):
    """How much a directory takes, in bytes. It follows the symlink."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                st = os.lstat(os.path.join(root, name))
                total += st.st_size
            except OSError:
                pass
    return total


def ic_cloud_root():
    """The iCloud Drive container, if it is there."""
    if os.path.isdir(ICLOUD_CONTAINER) and os.access(ICLOUD_CONTAINER, os.W_OK):
        return ICLOUD_CONTAINER
    return None


def is_remote(path):
    """Is this path inside the iCloud container?"""
    try:
        return os.path.realpath(path).startswith(os.path.realpath(ICLOUD_CONTAINER))
    except OSError:
        return False


def status():
    """What is going on, as a dictionary."""
    free = free_bytes(ROOT)
    return {
        "scratch": SCRATCH,
        "scratch_bytes": dir_size(SCRATCH) if os.path.isdir(SCRATCH) else 0,
        "scratch_is_remote": is_remote(SCRATCH),
        "ic_cloud": ic_cloud_root(),
        "free_bytes": free,
        "free_gib": round((free or 0) / GIB, 1),
        "warn_gib": FREE_WARN_GIB,
        "low": bool(free is not None and free < FREE_WARN_GIB * GIB),
    }


def _say(msg):
    print(msg, flush=True)


def relocate(yes=False):
    """Move the scratch space into the iCloud Drive and symlink it back."""
    if is_remote(SCRATCH):
        _say("the scratch is already on the iCloud Drive")
        return 0
    root = ic_cloud_root()
    if not root:
        _say("there is no iCloud Drive container on this Mac "
             "(expected " + ICLOUD_CONTAINER + ")")
        return 1
    if not os.path.isdir(SCRATCH):
        _say("there is no scratch directory yet: " + SCRATCH)
        return 1
    size = dir_size(SCRATCH)
    free = free_bytes(ROOT) or 0
    _say(f"the scratch is {size/GIB:.2f} GB and the disk has {free/GIB:.1f} GB free")
    if not yes:
        _say("this moves it to " + ICLOUD_SCRATCH + " and leaves a symlink "
             "behind. Run it again with --yes to do it.")
        return 2
    os.makedirs(os.path.dirname(ICLOUD_SCRATCH), exist_ok=True)
    # The move is a rename: the container is on the same volume, so it does
    # not copy a gigabyte anywhere.
    if os.path.exists(ICLOUD_SCRATCH):
        _say("there is already something at " + ICLOUD_SCRATCH)
        return 1
    shutil.move(SCRATCH, ICLOUD_SCRATCH)
    os.symlink(ICLOUD_SCRATCH, SCRATCH)
    _say("the scratch is now on the iCloud Drive:")
    _say("  " + SCRATCH + " -> " + ICLOUD_SCRATCH)
    _say("macOS will reclaim it locally when the disk gets tight.")
    return 0


def restore(yes=False):
    """Move the scratch space back onto the disk."""
    if not is_remote(SCRATCH):
        _say("the scratch is not on the iCloud Drive")
        return 0
    if not yes:
        _say("this moves it back to " + SCRATCH + ". Run it again with --yes.")
        return 2
    target = os.path.realpath(SCRATCH)
    os.unlink(SCRATCH)
    shutil.move(target, SCRATCH)
    _say("the scratch is back on the disk: " + SCRATCH)
    return 0


def ensure_space(min_free_gib=None, log=print):
    """Make room if the disk is nearly full.

    Called before a job starts, before its work directory exists, so
    nothing is moved out from under a running job. Returns a line saying
    what happened, or None.
    """
    limit = (min_free_gib if min_free_gib is not None else FREE_WARN_GIB) * GIB
    free = free_bytes(ROOT)
    if free is None or free >= limit:
        return None
    if is_remote(SCRATCH):
        return (f"the disk has {free/GIB:.1f} GB free and the scratch is "
                f"already on the iCloud Drive - macOS will reclaim it as "
                f"it needs to")
    log(f"the disk has only {free/GIB:.1f} GB free - moving the scratch "
        f"space onto the iCloud Drive")
    rc = relocate(yes=True)
    if rc == 0:
        return ("the scratch space was moved onto the iCloud Drive; macOS "
                "will reclaim it locally as the disk fills up")
    return (f"the disk has only {free/GIB:.1f} GB free and the scratch "
            f"space could not be moved")


def main():
    args = [a for a in sys.argv[1:] if a != "--yes"]
    yes = "--yes" in sys.argv
    cmd = args[0] if args else "status"
    if cmd == "status":
        s = status()
        print("the scratch space")
        print(f"  where      {s['scratch']}"
              + ("  (on the iCloud Drive)" if s["scratch_is_remote"] else ""))
        print(f"  size       {s['scratch_bytes']/GIB:.2f} GB")
        print(f"  free       {s['free_gib']} GB"
              + ("   <- LOW" if s["low"] else ""))
        print(f"  iCloud     {s['ic_cloud'] or 'not available'}")
        return 0
    if cmd == "relocate":
        return relocate(yes=yes)
    if cmd == "restore":
        return restore(yes=yes)
    if cmd == "check":
        # For use in a hook: non-zero when the disk is nearly full.
        s = status()
        return 1 if s["low"] else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
