#!/usr/bin/env python3
"""Give the agents an API for this upscaler.

API Anything (https://github.com/goodnight000/api-anything) turns a website
into operations an agent can call: the agent learns what the site does from
the site's own requests, and the next agent gets it for free. It runs as an
MCP server, so Claude Code, Codex, Cursor, Gemini CLI and VS Code all get
the same operations.

This project has an API of its own, on 127.0.0.1:8790. This script teaches
API Anything what that API can do, so an agent can say

    upscale /Users/you/Movies/film.mov to 1080p with DLoRAL

and have it happen, instead of clicking through the GUI.

    python3 tools/api_anything.py install     # connect the agents
    python3 tools/api_anything.py ops         # what they can call
    python3 tools/api_anything.py call presets
    python3 tools/api_anything.py status

The cloud routes cost money, so the gate stays: an agent may ASK for the
price, but only the user can approve it. The approval is a one-time id the
server issued and the user agreed to, and the job has to carry it.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SPEC_SRC = os.path.join(ROOT, "api-anything", "upscaler-gui.json")
SITE = "upscaler-gui"
MCP = '{"command":"npx","args":["-y","api-anything","mcp"]}'
# The operations that start work rather than read it. They need the writes
# to be allowed, and they are the ones that cost money - so they also need
# an approval the user gave.
WRITE_OPS = {"run_local", "run_cloud"}


def home():
    """Where API Anything keeps its data."""
    return os.environ.get("API_ANYTHING_HOME",
                          os.path.expanduser("~/.api-anything"))


def npx(*args, timeout=600):
    """Run the API Anything CLI."""
    cmd = ["npx", "-y", "api-anything"] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return 127, "npx is not on the PATH - Node 22.13 or newer is needed"
    except subprocess.TimeoutExpired:
        return 124, "the command timed out"


def install_spec():
    """Put our operations where API Anything looks for them."""
    dest_dir = os.path.join(home(), "sites")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, SITE + ".json")
    if os.path.exists(dest):
        shutil.copy2(dest, dest + ".bak")
    shutil.copy2(SPEC_SRC, dest)
    return dest


def cmd_install(a):
    print("installing the operations for", SITE)
    dest = install_spec()
    print("  " + dest)

    if a.agents:
        print("connecting the agents (sharing stays OFF: this is a local API,")
        print("and there is nothing to share - the operations point at 127.0.0.1)")
        code, out = npx("setup", "--no-share")
        print(out.strip()[-1500:] if out.strip() else "  (nothing to do)")
        if code != 0:
            print("  the setup did not finish cleanly (exit %d)" % code)
    else:
        print("not touching the agents (--no-agents)")

    code, out = npx("ops", SITE)
    print("\nthe operations the agents can call:")
    print(out.strip()[:1500])
    print("\ncheck it:")
    print("  python3 tools/api_anything.py call presets")
    return 0


def cmd_ops(a):
    code, out = npx("ops", SITE)
    print(out.strip())
    return 0 if code == 0 else code


def cmd_call(a):
    args = ["call", SITE, a.op] + list(a.args)
    if a.op in WRITE_OPS:
        # Starting work changes state, so the CLI needs to be told. The
        # money still needs the user's approval: a cloud run carries an
        # approval id the user agreed to.
        args.append("--allow-writes")
    code, out = npx(*args)
    print(out.strip())
    return code


def cmd_status(a):
    spec = os.path.join(home(), "sites", SITE + ".json")
    print("the operations:", "installed" if os.path.exists(spec) else "NOT installed",
          "at", spec)
    code, out = npx("ops", SITE)
    print("the CLI:", "works" if code == 0 else "not working")
    if out.strip():
        print(out.strip()[:800])
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:8790/api/presets",
                                    timeout=5) as r:
            body = r.read().decode()
        print("the upscaler:", "up", "with",
              len(json.loads(body).get("engines", [])), "engines")
    except Exception as e:
        print("the upscaler: not answering (%s)" % str(e)[:80])
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="install the operations and connect the agents")
    p.add_argument("--no-agents", dest="agents", action="store_false",
                   help="do not touch the agents' configuration")
    p.set_defaults(fn=cmd_install)
    p = sub.add_parser("ops", help="list the operations")
    p.set_defaults(fn=cmd_ops)
    p = sub.add_parser("call", help="call an operation")
    p.add_argument("op")
    p.add_argument("args", nargs="*")
    p.set_defaults(fn=cmd_call)
    p = sub.add_parser("status", help="what is installed and what is up")
    p.set_defaults(fn=cmd_status)
    a = ap.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
