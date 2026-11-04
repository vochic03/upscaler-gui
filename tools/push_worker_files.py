#!/usr/bin/env python3
"""Push the worker files to the public repo, so a rented box can fetch them.

The pod's own DNS is broken out of the box, so the worker cannot be typed
into it; it fetches these files from the public repo instead. The same
commit is what GitHub Actions builds into the CUDA image.
"""
import json
import os
import re
import subprocess
import sys
import time

OWNER = "vochic03"
REPO = "upscaler-gui"
BRANCH = "main"
WORK = "/tmp/gh-push"
BASE = "/Users/vasilijemicic/upscaler-gui"
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
        print(f"FAIL {label}: {err[:400]} {out[:300]}")
        return None
    print(f"ok   {label}")
    return out


def blob_sha(path):
    """The SHA of the file that is there now, if it is there."""
    res = subprocess.run(
        ["mcode-tools", "connector", "call", "connector__github__get_file_contents",
         "--args", json.dumps({"owner": OWNER, "repo": REPO, "path": path,
                               "ref": BRANCH})],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = (res.stdout or b"").decode("utf-8", "replace")
    if res.returncode != 0:
        return None
    try:
        data = json.loads(out)
    except Exception:
        return None
    # the answer comes back as content blocks; the SHA is in the first one
    if isinstance(data, list):
        for block in data:
            if isinstance(block, dict) and block.get("type") == "text":
                m = re.search(r"SHA:\s*([0-9a-f]{40})", block.get("text", ""))
                if m:
                    return m.group(1)
        data = data[0] if data else {}
    sha = data.get("sha") if isinstance(data, dict) else None
    return sha if isinstance(sha, str) else None


FILES = [
    "worker/Dockerfile",
    "worker/server.py",
    "worker/arch_models.py",
    "worker/realesrgan_cuda.py",
    "worker/serverless.py",
    "rental.py",
    "cloud.py",
    "tools/batch_run.py",
    "ENGINES.md",
    "README.md",
]

for path in FILES:
    with open(os.path.join(BASE, path), encoding="utf-8") as fh:
        content = fh.read()
    args = {"owner": OWNER, "repo": REPO, "path": path, "content": content,
            "message": "the CUDA path for the rented card: " + path,
            "branch": BRANCH}
    sha = blob_sha(path)
    if sha:
        args["sha"] = sha
    out = call("connector__github__create_or_update_file", args, "commit " + path)
    if out is None:
        sys.exit(1)
    time.sleep(1)

# and start the image build
call("connector__github__actions_run_trigger",
     {"method": "run_workflow", "owner": OWNER, "repo": REPO,
      "workflow_id": "image.yml", "ref": BRANCH},
     "trigger the image build")
print("done")
