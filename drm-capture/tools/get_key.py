#!/usr/bin/env python3
"""
Extract the content key for a Widevine stream using a local .wvd device.

    ./get_key.py <init-or-manifest-url> <kid-hex> <license-url> [wvd-dir]

Does the whole exchange locally with pywidevine's CDM:
    device -> CDM session -> license challenge -> POST to the license server
    -> parse license -> content key

Prints "KID <hex> KEY <hex>" for every key it recovers.
"""

import glob
import os
import struct
import sys
import urllib.error
import urllib.request
import uuid

from pywidevine import Cdm, Device, PSSH

WIDEVINE_UUID = "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"

HEADERS = {
    "Referer": "https://mneplay.me/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
}


def fmt(v):
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return str(v).replace("-", "")


def post(url, data, headers=None):
    h = dict(HEADERS)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def try_device(wvd_path, kid_hex, license_url, privacy_mode=False):
    try:
        device = Device.load(wvd_path)
        cdm = Cdm.from_device(device)
        sid = cdm.open()
        pssh = PSSH.new(system_id=uuid.UUID(WIDEVINE_UUID), key_ids=[uuid.UUID(kid_hex)])
        challenge = cdm.get_license_challenge(sid, pssh, "STREAMING", privacy_mode=privacy_mode)
        resp = post(license_url, challenge, {"Content-Type": "application/octet-stream"})
        if not resp:
            return None, "empty license response"
        cdm.parse_license(sid, resp)
        keys = cdm.get_keys(sid)
        if not keys:
            return None, "no keys in license"
        return keys, None
    except urllib.error.HTTPError as e:
        body = e.read()[:200]
        return None, f"HTTP {e.code} {body!r}"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    kid_hex = sys.argv[2].replace("-", "").strip()
    license_url = sys.argv[3]
    wvd_dir = sys.argv[4] if len(sys.argv) > 4 else os.path.join(os.path.dirname(__file__), "devices")

    devices = sorted(glob.glob(os.path.join(wvd_dir, "*.wvd")))
    if not devices:
        print(f"no .wvd files in {wvd_dir}")
        return 1

    print(f"{len(devices)} device(s) to try")
    for path in devices:
        name = os.path.basename(path)
        keys, err = try_device(path, kid_hex, license_url)
        if keys:
            for k in keys:
                print(f"\n*** SUCCESS with {name} ***")
                print(f"KID {fmt(k.kid)}")
                print(f"KEY {fmt(k.key)}")
                print(f"TYPE {getattr(k, 'type_', '')}")
            return 0
        print(f"  {name}: {err}")
    print("\nno device worked")
    return 1


if __name__ == "__main__":
    sys.exit(main())
