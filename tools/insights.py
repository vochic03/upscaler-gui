#!/usr/bin/env python3
"""What the coding has actually cost, and where the time went.

OpenRouter's observability page is the same feed this reads: every request
that went through OpenRouter, with its model, its tokens, its cost and how
long it took. Read as a dashboard it is a bill. Read as a loop it is
feedback: which of my habits are expensive, which model is worth its
price, and whether the last change was worth what it cost.

That feedback is what makes the next decision cheaper. A session that
spent four dollars and six hours on a full rebuild that could have been a
forty-five second patch is visible here, and it is not going to happen
twice in a row.

    python3 tools/insights.py            # the last day
    python3 tools/insights.py --days 7   # the last week
    python3 tools/insights.py --by none  # no grouping

The key is read from the macOS Keychain (or OPENROUTER_API_KEY). It is
never printed, never written to a file, and never asked for in chat.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://openrouter.ai/api/v1"


def api_key():
    """The OpenRouter key: the environment first, then the Keychain."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    try:
        import subprocess
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "openrouter", "-w"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        key = out.stdout.decode().strip()
        if key:
            return key
    except Exception:
        pass
    return None


def get(path, key):
    req = urllib.request.Request(
        API + path, headers={"Authorization": "Bearer " + key,
                             "User-Agent": "upscaler-gui/insights"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode() or "{}")


def rows_from(payload):
    """The activity feed, whatever shape it arrives in."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "activity", "generations", "items"):
            v = payload.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return []


def number(row, *names):
    for n in names:
        v = row.get(n)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                pass
    return 0.0


def tokens(row):
    u = row.get("usage") or {}
    if not isinstance(u, dict):
        u = {}
    return (number(u, "prompt_tokens", "input_tokens")
            + number(u, "completion_tokens", "output_tokens"))


def latency(row):
    """How long it took, in seconds."""
    for n in ("latency", "generation_time", "duration", "elapsed"):
        v = number(row, n)
        if v:
            return v
    t = row.get("timings") or {}
    if isinstance(t, dict):
        total = number(t, "total")
        if total:
            return total
        prompt = number(t, "prompt")
        completion = number(t, "completion")
        if prompt or completion:
            return prompt + completion
    return 0.0


def money(v):
    return "$" + ("%.4f" % v)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=1.0,
                    help="how far back to look (days)")
    ap.add_argument("--by", default="model", choices=("model", "none"),
                    help="group the totals by model")
    ap.add_argument("--limit", type=int, default=25, help="rows to print")
    a = ap.parse_args()

    key = api_key()
    if not key:
        print("no OpenRouter key on this machine.")
        print("Put one in the Keychain - nothing has to be pasted into a chat:")
        print("  security add-generic-password -a \"$USER\" -s openrouter -w")
        print("or export it for one shell: OPENROUTER_API_KEY=...")
        return 0

    try:
        payload = get("/activity", key)
    except urllib.error.HTTPError as e:
        print("OpenRouter said %s" % e.code)
        if e.code == 401:
            print("the key was refused - check the Keychain entry")
        return 1
    except Exception as e:
        print("could not reach OpenRouter: %s" % str(e)[:120])
        return 1

    rows = rows_from(payload)
    cutoff = time.time() - a.days * 86400
    recent = []
    for r in rows:
        created = r.get("created_at") or r.get("created") or 0
        try:
            created = float(created)
            if created > 1e12:
                created /= 1000.0
        except (TypeError, ValueError):
            created = 0
        if not created or created >= cutoff:
            recent.append((created, r))
    recent.sort(key=lambda x: x[0], reverse=True)

    total_cost = sum(number(r, "cost", "total_cost") for _, r in recent)
    total_tok = sum(tokens(r) for _, r in recent)
    total_time = sum(latency(r) for _, r in recent)

    print("%d request(s) in the last %g day(s)" % (len(recent), a.days))
    print("  cost     " + money(total_cost))
    print("  tokens   %s" % format(total_tok, ",.0f"))
    print("  time     %.1f min of model time" % (total_time / 60.0))
    if recent:
        print("  average  %s and %.1f s a request" % (money(total_cost / len(recent)), total_time / len(recent)))

    if a.by == "model":
        per = {}
        for _, r in recent:
            m = r.get("model") or r.get("model_name") or "?"
            e = per.setdefault(m, [0.0, 0, 0.0])
            e[0] += number(r, "cost", "total_cost")
            e[1] += tokens(r)
            e[2] += latency(r)
        if per:
            print("\nby model:")
            for m, (c, t, s) in sorted(per.items(), key=lambda x: -x[1][0]):
                print("  %-44s %s  %10s tok  %7.1f min" % (m[:44], money(c), format(t, ",.0f"), s / 60.0))

    print("\nthe most expensive requests:")
    for created, r in sorted(recent, key=lambda x: -number(x[1], "cost", "total_cost"))[:a.limit]:
        model = r.get("model") or r.get("model_name") or "?"
        cost = number(r, "cost", "total_cost")
        if cost <= 0:
            continue
        print("  %s %6.1fs %9s tok  %s" % (money(cost), latency(r), format(tokens(r), ",.0f"), model[:40]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
