#!/usr/bin/env python3
"""Watch the batch requests, with the endpoint switched on for a while.

The batch API runs on workers of its own and Runpod schedules them when
there is spare capacity, so a batch that was finalised hours ago can sit
with requestCompleted at zero for a long time. This turns the endpoint on
for a fixed window, watches the counters, and parks it again afterwards.

    python3 tools/batch_probe.py --endpoint a76yvmlq4f5q6j --minutes 10

The window is the whole budget: the endpoint is switched off in a finally
block, so an interrupted run still stops billing.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import keys  # noqa: E402

API = "https://api.runpod.ai/v2"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def call(method, path, key, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API}/{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"{method} {path} -> {e.code}: {detail}")


def workers_on(ep, key, n):
    ok = _patch_workers(ep, key, n)
    if ok:
        print(f"endpoint {ep}: workers.max = {n}")
    else:
        print(f"endpoint {ep}: could not set workers.max = {n} "
              f"(park it by hand!)", file=sys.stderr)


def workers_off(ep, key):
    ok = _patch_workers(ep, key, 0)
    if ok:
        print(f"endpoint {ep}: workers.max = 0  (parked, nothing bills)")
    else:
        print(f"WARNING: could not park {ep} - set workers.max to 0 by hand",
              file=sys.stderr)


def _patch_workers(ep, key, n):
    """The v2 REST API has no PATCH on the endpoint itself, so try the
    shapes that do work and say so when none of them do."""
    body = {"workers": {"min": 0, "max": n, "idleTimeout": 5}}
    for path in (f"serverless/{ep}", ep):
        try:
            call("PATCH", path, key, body)
            return True
        except Exception:
            continue
    # GraphQL: mutateEndpoint is what the console talks to.
    query = ("mutation { mutateEndpoint(input: {id: \"%s\", "
             "workers: {min: 0, max: %d, idleTimeout: 5}}) { id } }" % (ep, n))
    req = urllib.request.Request("https://api.runpod.ai/graphql",
                                 data=json.dumps({"query": query}).encode(),
                                 method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
        if "errors" in body:
            print(f"graphql said: {body['errors'][0].get('message')}",
                  file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f"graphql park failed: {e}", file=sys.stderr)
        return False


def snapshot(ep, key):
    """What is queued, what is running, what the batch counters say."""
    out = {"batches": [], "workers": "?", "jobs": "?"}
    try:
        for b in call("GET", f"{ep}/batch", key):
            out["batches"].append({
                "id": b.get("id", "")[:8], "full": b.get("id"),
                "status": b.get("status"),
                "total": b.get("requestTotal"), "done": b.get("requestCompleted"),
                "failed": b.get("requestFailed"),
                "running": b.get("requestInProgress")})
    except Exception as e:
        out["batches"] = [f"error: {e}"]
    try:
        h = call("GET", f"{ep}/health", key)
        out["workers"] = h.get("workers")
        out["jobs"] = h.get("jobs")
    except Exception as e:
        out["workers"] = f"error: {e}"
    return out


def request_status(ep, key, batch_id):
    try:
        page = call("GET", f"{ep}/batch/{batch_id}/requests", key)
        return [(r.get("id", "")[-12:], r.get("status")) for r in page.get("requests", [])]
    except Exception as e:
        return [("error", str(e))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="a76yvmlq4f5q6j")
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--max-workers", type=int, default=1)
    ap.add_argument("--price-per-hour", type=float, default=0.89)
    ap.add_argument("--every", type=float, default=45.0)
    ap.add_argument("--manage", action="store_true",
                    help="also try to switch the workers on and off "
                         "(the v2 REST API has no PATCH on an endpoint, so "
                         "this usually fails - park it from the console or "
                         "the GraphQL API instead)")
    args = ap.parse_args()

    key = keys.resolve("runpod")
    if not key:
        print("no Runpod key in the keychain", file=sys.stderr)
        return 1

    deadline = time.time() + args.minutes * 60
    t0 = time.time()
    try:
        if args.manage:
            workers_on(args.endpoint, key, args.max_workers)
        else:
            print("watching only - switch the endpoint on before, and park "
                  "it again after")
        seen_done = 0
        while time.time() < deadline:
            time.sleep(min(args.every, max(1.0, deadline - time.time())))
            snap = snapshot(args.endpoint, key)
            spent = time.time() - t0
            print(f"[{spent:6.0f}s] workers={snap['workers']} jobs={snap['jobs']}")
            for b in snap["batches"]:
                print(f"          batch {b}")
                if isinstance(b, dict) and b.get("full") and b.get("total"):
                    for rid, st in request_status(args.endpoint, key, b["full"]):
                        print(f"            request {rid} {st}")
            done = sum(b.get("done", 0) for b in snap["batches"] if isinstance(b, dict))
            if done > seen_done:
                seen_done = done
                print(f"          >>> {done} request(s) finished")
                if all((b.get("done", 0) + b.get("failed", 0)) == b.get("total", 0)
                       for b in snap["batches"] if isinstance(b, dict) and b.get("total")):
                    print("          everything in every batch is done")
                    break
        spent = time.time() - t0
        print(f"\nwindow over after {spent:.0f}s  (billable ceiling "
              f"${spent / 3600 * args.price_per_hour:.3f})")
    finally:
        if args.manage:
            workers_off(args.endpoint, key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
