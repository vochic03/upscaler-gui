#!/usr/bin/env python3
"""The network volume the weights live on.

The volume is the answer to "every run downloads eleven gigabytes again":
the weights are fetched onto it once and every card that mounts it finds
them already there. It costs $0.07 per GB per month.

But a volume only lives in one data centre, and a card can only mount a
volume from its own data centre - so where the volume sits decides which
cards can use it. A volume in a centre without cards is $2.80 a month for
nothing, which is exactly what happened the first time: it was created in
US-NC-1, which had no GPU of any kind.

    python3 tools/volume.py list
    python3 tools/volume.py probe            # which centres have what
    python3 tools/volume.py create --dc US-TX-3 --size 40
    python3 tools/volume.py delete --id gb5ouapvqq

"probe" costs nothing: a pod that cannot be created is not billed, and
Runpod answers "no instances available" before it looks at the balance -
so the difference between that answer and "the balance is too low" tells
you where there are cards.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import keys  # noqa: E402

API = "https://api.runpod.io/v2"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# The cards the diffusion engines need, and the ones the local routes use.
CARDS = ["NVIDIA L40S", "NVIDIA A100 PCIe", "NVIDIA H100 PCIe",
         "NVIDIA GeForce RTX 4090"]


def _request(method, path, body=None, api_key=None):
    api_key = api_key or keys.resolve("runpod")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + api_key,
                                          "Content-Type": "application/json",
                                          "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"detail": raw[:300]}


def volumes(api_key=None):
    st, data = _request("GET", "/network-volumes", api_key=api_key)
    if st != 200:
        raise SystemExit("cannot list the volumes: " + json.dumps(data)[:300])
    return data.get("networkVolumes") or []


def volume_centres(api_key=None):
    """The data centres that take a network volume.

    Runpod says the list itself when it refuses a volume in a centre that
    does not take them, which is a good deal cheaper than trying them one
    by one.
    """
    st, data = _request("POST", "/network-volumes",
                         {"name": "probe-centres", "size": 40,
                          "dataCenter": "XX-XX-9"}, api_key=api_key)
    detail = (data.get("detail") or "") if isinstance(data, dict) else str(data)
    if "Available data centers:" not in detail:
        return []
    tail = detail.split("Available data centers:", 1)[1]
    tail = tail.split('"')[0]
    return [x.strip() for x in tail.split(",") if x.strip()]


def probe_card(dc, gpu, api_key=None):
    """What happens if we ask for one card in that centre.

    "none"    - the centre has no card of that kind
    "free"    - there is one, and the account cannot pay for it
    "created" - there is one and we could have rented it
    """
    body = {"name": "probe", "image": "docker.io/bzz1/upscaler:both",
            "gpu": {"id": gpu, "count": 1}, "disk": 20,
            "ports": ["8000/http"], "globalNetworking": True,
            "dataCenterIds": [dc]}
    st, data = _request("POST", "/pods", body, api_key=api_key)
    if st == 200:
        pid = data.get("id")
        _request("DELETE", "/pods/" + pid, api_key=api_key)
        return "created (destroyed at once)"
    detail = (data.get("detail") or "") if isinstance(data, dict) else str(data)
    if "instances available" in detail:
        return "none"
    if "balance is too low" in detail:
        return "free"
    if "Global networking" in detail:
        return "no global networking"
    return f"error {st}"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", help="the Runpod key (default: the keychain)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="the volumes on the account")
    p = sub.add_parser("probe", help="where there are cards (costs nothing)")
    p.add_argument("--card", action="append", help="a card to look for")
    p.add_argument("--centres", help="comma separated data centres")
    p = sub.add_parser("create", help="make a volume")
    p.add_argument("--dc", required=True, help="the data centre")
    p.add_argument("--size", type=int, default=40, help="gigabytes")
    p.add_argument("--name", default="upscaler-weights")
    p = sub.add_parser("delete", help="throw a volume away")
    p.add_argument("--id", required=True)

    a = ap.parse_args()
    api_key = a.key or keys.resolve("runpod")

    if a.cmd == "list":
        found = volumes(api_key)
        if not found:
            print("there are no volumes on this account")
        for v in found:
            print(f"  {v.get('id')}  {v.get('name')}  {v.get('size')} GB  "
                  f"{v.get('type')}  {v.get('dataCenter')}")
        return

    if a.cmd == "probe":
        centres = ([c.strip() for c in a.centres.split(",")]
                   if a.centres else volume_centres(api_key))
        cards = a.card or CARDS
        print(f"{len(centres)} data centres take a network volume\n")
        for dc in centres:
            row = []
            for gpu in cards:
                row.append(f"{gpu.split()[-1]}: {probe_card(dc, gpu, api_key)}")
            print(f"  {dc:10} " + "  ".join(row), flush=True)
        return

    if a.cmd == "create":
        st, data = _request("POST", "/network-volumes",
                             {"name": a.name, "size": a.size,
                              "dataCenter": a.dc}, api_key=api_key)
        if st not in (200, 201):
            raise SystemExit("could not create the volume: " + json.dumps(data)[:300])
        print(f"created {data.get('id')}  {data.get('name')}  "
              f"{data.get('size')} GB  {data.get('dataCenter')}")
        print("put this id in rental.py: DEFAULT_VOLUME_ID = " +
              repr(data.get("id")))
        return

    if a.cmd == "delete":
        st, data = _request("DELETE", "/network-volumes/" + a.id, api_key=api_key)
        if st not in (200, 204):
            raise SystemExit("could not delete it: " + json.dumps(data)[:300])
        print("deleted " + a.id)


if __name__ == "__main__":
    main()
