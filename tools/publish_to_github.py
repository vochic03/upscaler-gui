#!/usr/bin/env python3
"""Put the worker image build on GitHub Actions.

The point: a GitHub Actions workflow pushes to ghcr.io with the workflow's own
GITHUB_TOKEN, so the image can be built and published without a personal
access token and without anybody logging in to Docker Hub.

Steps: create the repo, commit the Dockerfile, the worker and the workflow,
then trigger the build.
"""
import json
import os
import subprocess
import sys
import time

OWNER = "vochic03"
REPO = "upscaler-gui"
BRANCH = "main"
WORK = "/tmp/gh-push"
os.makedirs(WORK, exist_ok=True)


def call(tool, args, label):
    path = os.path.join(WORK, "args.json")
    with open(path, "w") as fh:
        json.dump(args, fh)
    res = subprocess.run(["mcode-tools", "connector", "call", tool,
                           "--args-file", path],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = (res.stdout or b"").decode("utf-8", "replace")
    err = (res.stderr or b"").decode("utf-8", "replace")
    if res.returncode != 0:
        print(f"FAIL {label}: {err[:300]} {out[:200]}")
        return None
    print(f"ok   {label}")
    return out


WORKFLOW = """name: build the worker image

on:
  workflow_dispatch:
  push:
    branches: [main]

permissions:
  contents: read
  packages: write

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/build-push-action@v6
        with:
          context: worker
          platforms: linux/amd64
          push: true
          tags: |
            ghcr.io/${{ github.repository_owner }}/upscaler:latest
            ghcr.io/${{ github.repository_owner }}/upscaler:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
"""

# 1. the repository
out = call("connector__github__create_repository",
           {"name": REPO, "private": False, "autoInit": True,
            "description": "The video upscaler: a local Mac GUI, a worker that "
                           "runs the AI pass on a rented GPU, and the image "
                           "both of them talk to."},
           "create the repository")
if out is None:
    sys.exit(1)
time.sleep(2)

# 2. the files
files = [
    ("worker/Dockerfile",
     open("/Users/vasilijemicic/upscaler-gui/worker/Dockerfile",
          encoding="utf-8").read()),
    ("worker/server.py",
     open("/Users/vasilijemicic/upscaler-gui/worker/server.py",
          encoding="utf-8").read()),
    (".github/workflows/image.yml", WORKFLOW),
]
for path, content in files:
    out = call("connector__github__create_or_update_file",
               {"owner": OWNER, "repo": REPO, "path": path, "content": content,
                "message": "add " + path, "branch": BRANCH},
               "commit " + path)
    if out is None:
        sys.exit(1)
    time.sleep(1)

# 3. build it
time.sleep(3)
call("connector__github__actions_run_trigger",
     {"method": "run_workflow", "owner": OWNER, "repo": REPO,
      "workflow_id": "image.yml", "ref": BRANCH},
     "trigger the build")
print("done - the image will be ghcr.io/%s/upscaler:latest" % OWNER)
