#!/usr/bin/env python3
"""Tidy the repository: drop the probe files, keep the build recipe.

The connector refuses to write under .github/workflows/ (a deliberate guard
against an agent injecting CI), so the workflow lives at ci/build-image.yml
and the user copies it into .github/workflows/ from the GitHub UI if they
want GitHub to publish the image for them.
"""
import json
import os
import subprocess
import time

OWNER, REPO, BRANCH = "vochic03", "upscaler-gui", "main"
WORK = "/tmp/gh-push"
os.makedirs(WORK, exist_ok=True)


def call(tool, args, label, show=0):
    path = os.path.join(WORK, "a.json")
    json.dump(args, open(path, "w"))
    r = subprocess.run(["mcode-tools", "connector", "call", tool, "--args-file", path],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace")
    if r.returncode != 0:
        print(f"FAIL {label}: {err[:160]}")
        return None
    if show:
        print(f"     {out[:show]}")
    return out


def text_of(raw):
    try:
        d = json.loads(raw)
        parts = [p.get("text", "") for p in d if p.get("type") == "text"]
        return json.loads("".join(parts))
    except Exception:
        return None


def listing(path="/"):
    raw = call("connector__github__get_file_contents",
               {"owner": OWNER, "repo": REPO, "path": path}, "list " + path)
    return text_of(raw) if raw else None


def sha_of(path):
    lst = listing(path)
    if not lst:
        return None
    if isinstance(lst, list):
        for f in lst:
            if f.get("name") == path:
                return f.get("sha")
    return lst.get("sha")


# 1. remove the probe files
for path in (".keep.txt", ".github/keep.txt", "x/.github/y.txt", "workflows/image.yml"):
    sha = sha_of(path)
    if not sha:
        print(f"skip {path} (not found)")
        continue
    call("connector__github__delete_file",
         {"owner": OWNER, "repo": REPO, "path": path, "sha": sha,
          "message": "remove a probe file", "branch": BRANCH}, "delete " + path)
    time.sleep(1)

# 2. keep the build recipe where the connector can write it
wf = open("/Users/vasilijemicic/upscaler-gui/tools/publish_to_github.py",
          encoding="utf-8").read()
import re
workflow = re.search(r'WORKFLOW = """(.*?)"""', wf, re.S).group(1)
call("connector__github__create_or_update_file",
     {"owner": OWNER, "repo": REPO, "path": "ci/build-image.yml",
      "content": workflow, "message": "keep the image build recipe here",
      "branch": BRANCH}, "commit ci/build-image.yml")
time.sleep(1)

# 3. a README that says what this is
readme = """# upscaler-gui

The video upscaler. A small local GUI on the Mac drives everything; the AI
pass runs on a rented GPU in a container; the finishing pass (blend, soften,
grain, grade, encode) stays on the Mac because it is cheap CPU work.

## The worker image

`worker/Dockerfile` builds the image the GPU box runs:

    docker build --platform linux/amd64 -t upscaler:latest worker/

It carries Real-ESRGAN and RIFE. Three optional stages are behind build
arguments: `--build-arg BUILD_DLORAL=1`, `BUILD_SEEDVR2=1`, `BUILD_STAR=1`.

`ci/build-image.yml` is a GitHub Actions workflow that builds the same image
and pushes it to ghcr.io with the workflow's own token, so no personal access
token is needed. Copy it to `.github/workflows/` to use it.

## The finishing pass

The grain and the halation run as a second pass over the finished file, not
inside the encoder: a second filter input inside the piped encoder stalls the
framesync, because the frames arrive in bursts and the framesync pulls the
other input to fill every gap.
"""
sha = sha_of("README.md")
call("connector__github__create_or_update_file",
     {"owner": OWNER, "repo": REPO, "path": "README.md", "content": readme,
      "message": "describe the repository",
      **({"sha": sha} if sha else {}),
      "branch": BRANCH}, "commit README.md")

print("\n--- the repository now ---")
lst = listing("/")
if isinstance(lst, list):
    for f in lst:
        print(f"  {f.get('type'):5} {f.get('path')}")
