#!/usr/bin/env python3
"""Pull Selur's forum into plain text.

The forum is MyBB, which gives us two things that need no login:
  * archive/index.php/forum-N.html  - every thread of a forum, in one page
  * archive/index.php/thread-N.html - the whole thread, stripped down
  * syndication.php?fid=N           - the same content as RSS

Usage:
    python3 selur_fetch.py forums            # list every forum and its threads
    python3 selur_fetch.py threads 2 8       # list the threads of those forums
    python3 selur_fetch.py pull 2 8          # download those threads to text
"""
import argparse
import html
import os
import re
import sys
import time
import urllib.request

BASE = "https://forum.selur.net"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
OUT = os.path.expanduser("~/upscaler-gui/work/selur")


def get(url, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", UA)
            req.add_header("Accept", "text/html,application/xhtml+xml,*/*")
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            if attempt == tries - 1:
                print(f"  !! {url}: {e}", file=sys.stderr)
                return ""
            time.sleep(2)


def strip_tags(page):
    page = re.sub(r"(?is)<(script|style).*?</\1>", " ", page)
    page = re.sub(r"(?i)<br\s*/?>", "\n", page)
    page = re.sub(r"(?i)</(p|div|li|tr|h\d)>", "\n", page)
    page = re.sub(r"<[^>]+>", " ", page)
    page = html.unescape(page)
    page = re.sub(r"[ \t\xa0]+", " ", page)
    page = re.sub(r"\n\s*\n\s*\n+", "\n\n", page)
    return page.strip()


def forum_threads(fid):
    """Every thread of a forum, from the archive (one page holds them all)."""
    page = get(f"{BASE}/archive/index.php/forum-{fid}.html")
    out = []
    for m in re.finditer(r'href="([^"]*thread-(\d+)\.html)"[^>]*>(.*?)</a>', page, re.S):
        url, tid, title = m.group(1), m.group(2), strip_tags(m.group(3))
        title = re.sub(r"\s+", " ", title).strip()
        if title and not any(t[0] == tid for t in out):
            out.append((tid, title, url if url.startswith("http") else BASE + url))
    return out


def thread_text(tid, url=None):
    url = url or f"{BASE}/archive/index.php/thread-{tid}.html"
    return strip_tags(get(url))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["forums", "threads", "pull"])
    ap.add_argument("fids", nargs="*", type=int)
    ap.add_argument("--grep", help="only keep threads whose text matches this")
    args = ap.parse_args()

    if args.mode == "forums":
        page = get(f"{BASE}/archive/index.php")
        for m in re.finditer(r'href="([^"]*forum-(\d+)\.html)"[^>]*>(.*?)</a>', page, re.S):
            print(f"forum {m.group(2):>2}: {strip_tags(m.group(3))}")
        return 0

    fids = args.fids or list(range(1, 9))
    os.makedirs(OUT, exist_ok=True)
    hits = []
    for fid in fids:
        threads = forum_threads(fid)
        print(f"\n=== forum {fid}: {len(threads)} threads ===")
        for tid, title, url in threads:
            print(f"  {tid:>6}  {title[:110]}")
            if args.mode == "pull":
                text = thread_text(tid, url)
                if args.grep and not re.search(args.grep, text, re.I):
                    continue
                path = os.path.join(OUT, f"forum{fid}-thread{tid}.txt")
                with open(path, "w") as fh:
                    fh.write(f"URL: {url}\nTITLE: {title}\n\n{text}\n")
                print(f"          -> {path}")
                hits.append((fid, tid, title))
                time.sleep(0.7)
    return 0


if __name__ == "__main__":
    sys.exit(main())
