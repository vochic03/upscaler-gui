#!/usr/bin/env python3
"""
Where the rental keys live, and how they are kept.

A rental API key can spend money - it can start a GPU and keep it running -
so it is treated like a password, not like a setting:

  * the value is never written into the log, the job record or any file the
    project ships;
  * the GUI does not have to hold it at all: it can just say which stored
    key to use, and the server resolves it at the moment the job starts;
  * the stored copy lives somewhere only you can read - a 0600 file, or the
    macOS Keychain.

Three places are searched, in this order:

  1. an environment variable, for one-off runs:
         export UPSCALER_RENT_KEY=...
  2. a file, e.g. ~/upscaler-gui/keys/runpod.key  (mode 0600)
  3. the macOS Keychain:
         security add-generic-password -a "$USER" -s upscaler-runpod -w
     (and the keychain prompts once, then releases it to this process)

`resolve()` returns the secret or None. It never prints it.
"""

import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_DIR = os.path.join(HERE, "keys")

# The services the tool can rent from, and where their keys live.
PROVIDERS = {
    "runpod": {"env": "UPSCALER_RENT_KEY", "file": "runpod.key",
               "keychain": "upscaler-runpod",
               "console": "https://console.runpod.io/user/settings/api"},
    "deepinfra": {"env": "UPSCALER_RENT_KEY_DEEPINFRA", "file": "deepinfra.key",
                  "keychain": "upscaler-deepinfra",
                  "console": "https://deepinfra.com/dash/api_keys"},
    "fal": {"env": "UPSCALER_RENT_KEY_FAL", "file": "fal.key",
            "keychain": "upscaler-fal",
            "console": "https://fal.ai/dashboard/keys"},
}


def _from_keychain(service):
    """Read a password out of the macOS Keychain (no output unless it works)."""
    if os.name != "nt":
        try:
            res = subprocess.run(
                ["security", "find-generic-password", "-a", os.environ.get("USER", ""),
                 "-s", service, "-w"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
            if res.returncode == 0:
                value = res.stdout.decode("utf-8", "replace").strip()
                if value:
                    return value
        except Exception:
            pass
    return None


def _from_file(name):
    path = os.path.join(KEY_DIR, name)
    try:
        st = os.stat(path)
        if st.st_mode & 0o077:
            print(f"warning: {path} is readable by others - chmod 600 it")
        with open(path) as fh:
            value = fh.read().strip()
        return value or None
    except OSError:
        return None


def resolve(provider="runpod"):
    """The key for a provider, or None. The value is never logged."""
    spec = PROVIDERS.get(provider)
    if not spec:
        return None
    value = os.environ.get(spec["env"])
    if value:
        return value.strip()
    value = _from_file(spec["file"])
    if value:
        return value
    return _from_keychain(spec["keychain"])


def where(provider="runpod"):
    """A description of where the key can be found. Safe to print."""
    spec = PROVIDERS.get(provider)
    if not spec:
        return []
    return [
        f"environment variable  {spec['env']}",
        f"file                 {os.path.join(KEY_DIR, spec['file'])}",
        f"keychain             security add-generic-password -a \"$USER\" -s {spec['keychain']} -w",
        f"create one at        {spec['console']}",
    ]


def redact(text, *secrets):
    """Strip any secret that might have ended up in a message."""
    out = str(text)
    for s in secrets:
        if s and len(s) > 6:
            out = out.replace(s, "[redacted]")
    return out
