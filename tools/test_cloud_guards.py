#!/usr/bin/env python3
"""The guards, checked.

Three faults cost one rental each, because nothing refused early:

  1. the DLoRAL preset asked for 4x with no model, the server filled in the
     default 2x model and refused the scale - so the preset could not start
     at all;
  2. the estimate showed its duration in seconds, so a 47 hour job read as
     a number next to a price;
  3. the worker answered /health with every engine it could ever have, so
     the job was told yes and only found out after the denoise and after a
     pod had been rented.

This checks that all three stay fixed, and that a refused job really does
not rent anything.

    python3 tools/test_cloud_guards.py
"""
import json
import shutil
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import keys  # noqa: E402

GUI = "http://127.0.0.1:8790"
FAILED = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + (("  - " + detail) if detail else ""))
    if not ok:
        FAILED.append(name)


def call(path, body=None, method=None, timeout=60, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GUI + path, data=data,
                                 method=method or ("POST" if body else "GET"))
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def page_token():
    """The approval token, from the GUI page - the way the GUI gets it.

    Approving a price is the user's act, and the token is handed out only
    inside the page the user looks at. This suite plays the user here.
    """
    import re
    req = urllib.request.Request(GUI + "/")
    req.add_header("User-Agent", "Mozilla/5.0")
    with urllib.request.urlopen(req, timeout=20) as r:
        html = r.read().decode("utf-8", "replace")
    m = re.search(r'name="approve-token" content="([0-9a-f]+)"', html)
    return m.group(1) if m else ""


def pods():
    """The pods the account is renting, through the same code that creates
    them (the pod API lives on api.runpod.io, not api.runpod.ai)."""
    import rental
    key = keys.resolve("runpod")
    if not key:
        return None
    try:
        return rental.RunPod(key)._request("GET", "/pods").get("pods", [])
    except Exception:
        return None


def main():
    print("the guards")

    # 1. the presets that need an engine the image does not have are marked
    status, models = call("/api/models")
    if status != 200:
        print("  FAIL the GUI is not answering on " + GUI)
        return 1
    presets = {p["id"]: p for p in models.get("presets", [])}
    # The regenerate preset needs DLoRAL, and the image it defaults to has
    # DLoRAL in it - so it can run as it is. It used to point at the image
    # WITHOUT the diffusion layer, which made the preset a dead end and
    # made the job refuse before it began.
    p = presets.get("regenerate")
    check("the regenerate preset can run with the default image",
          p is not None and p.get("available") is True,
          (p or {}).get("reason", "no preset"))
    p = presets.get("star")
    # The image the STAR preset defaults to is the one with STAR in it, so
    # it can run as it is. It used to point at the plain image, which has no
    # STAR, and the preset was a dead end.
    check("the star preset can run with the default image",
          p is not None and p.get("available") is True,
          (p or {}).get("reason", "no preset"))
    for pid in ("faithful", "polished", "cloud", "best"):
        p = presets.get(pid)
        check(f"the {pid} preset is available", p is not None
              and p.get("available") is not False)

    # 2. the estimate speaks in hours
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "work", "guard_test_clip.mp4")
    # The suite must not depend on a file that a cleanup can take away, and
    # it must not share one with the rest of the work. It makes its own clip
    # every time, so the numbers below are the numbers it checks.
    #
    # The clip has to be long enough for the checks to mean anything. The
    # pod spin-up (240 s) is paid for every job, so on a two second clip it
    # swamps the model time and every model looks the same price. And a
    # diffusion over a short clip comes out under an hour, which is no way
    # to check that the estimate speaks in hours. Five minutes puts both
    # back.
    # The clip is made fresh every time. If an earlier run left a file there
    # and macOS had evicted it to the iCloud, opening that file for writing
    # blocks on the download and the test HANGS instead of running - so the
    # old one goes first.
    if os.path.exists(src):
        try:
            os.remove(src)
        except OSError:
            pass
    os.makedirs(os.path.dirname(src), exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=300",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=300",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", src], check=False)
    if os.path.exists(src):
        status, est = call("/api/estimate", {"path": src, "backend": "cloud",
                                             "cloud_engine": "dloral", "model": "",
                                             "scale": 4, "look": "polished", "rent": True})
        check("the estimate comes back", status == 200)
        # Both diffusion engines have been measured on a card now, so the
        # estimate says so. An engine with no measurement still has to say
        # that it is a guess.
        check("the estimate says the engine was measured",
              est.get("measured") is True)
        status, est2 = call("/api/estimate", {"path": src, "backend": "cloud",
                                              "cloud_engine": "seedvr2", "model": "",
                                              "scale": 4, "look": "polished", "rent": True})
        check("the estimate says the other engine was measured too",
              est2.get("measured") is True)
        check("the estimate carries the duration",
              (est.get("ai_seconds") or 0) > 0)
        # the seconds must be readable as a duration
        secs = est.get("ai_seconds") or 0
        hours = secs / 3600.0
        check("the duration reads as hours, not as a number of seconds",
              hours >= 1.0, f"{secs}s = {hours:.1f} h")
    else:
        check("the test clip could be made", False, src)

    # 4. the estimate knows which net is doing the work: the deep RRDB net
    #    does about twenty times the work per pixel of the compact one
    if os.path.exists(src):
        def cost_for(model, scale):
            _, e = call("/api/estimate", {"path": src, "backend": "cloud",
                                          "cloud_engine": "realesrgan", "model": model,
                                          "scale": scale, "look": "polished", "rent": True})
            return (e.get("one_clip_cost") or 0), e.get("measured")
        compact, cm = cost_for("xintao-realesr-animevideo-x2-v3", 2)
        deep, dm = cost_for("xintao-realesrgan-x4plus", 4)
        check("the compact model is quoted as measured", cm is True)
        check("the deep model is quoted as a guess", dm is False)
        check("the deep model costs more than the compact one", deep > compact * 3,
              f"${compact} against ${deep}")

    # 3. a job that cannot run refuses, and rents nothing
    #
    # The image is named explicitly here. The default image for DLoRAL has
    # the diffusion layer in it - that is the point of the fix - so the
    # refusal has to be tested against an image that genuinely lacks it.
    before = pods()

    # A job that would rent a GPU needs the user's approval of the price,
    # and the approval is the server's: an agent cannot write its own.
    #
    # UNLESS a session spending cap has been set - which is how an agent is
    # allowed to spend without asking for every price. Then the cap is the
    # control instead of the human, and the server enforces it.
    _, spend = call("/api/spend")
    cap = float(spend.get("cap") or 0)
    if cap > 0:
        check("the session spending cap is set", True,
              "$%.2f of $%.2f used" % (spend.get("total") or 0, cap))
        check("the spend never goes over the cap",
              (spend.get("total") or 0) <= cap)
    else:
        status, refused = call("/api/run", {
            "path": src, "backend": "cloud", "cloud_engine": "dloral", "model": "",
            "scale": 4, "look": "polished", "denoise_engine": "bilateral",
            "encoder": "h264", "rent": True, "rent_approved": True,
            "rent_image": "docker.io/bzz1/upscaler:latest"})
        check("a job that rents without an approval is refused",
              status == 400 and not refused.get("id"),
              json.dumps(refused)[:160])
        check("the refusal asks for an estimate",
              bool(refused.get("needs_approval")), json.dumps(refused)[:120])

    # 3b1. The cap itself, on a server of its own: a job that would cost
    #      more than the cap allows is refused, and nothing is created.
    try:
        import socket as _socket
        import subprocess as _sp
        import urllib.error as _ue
        import urllib.request as _ur
        _s = _socket.socket()
        _s.bind(("127.0.0.1", 0))
        _port = _s.getsockname()[1]
        _s.close()
        _env = dict(os.environ)
        _env["UPSCALER_SPEND_CAP_USD"] = "0.0001"
        _env["UPSCALER_SPEND_FILE"] = "/tmp/upscaler-guard-spend.json"
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _logf = open("/tmp/upscaler-guard-server.log", "w")
        _proc = _sp.Popen([__import__("sys").executable, "server.py", "--port", str(_port)],
                           cwd=_root, env=_env,
                           stdout=_logf, stderr=_sp.STDOUT)
        _base = "http://127.0.0.1:%d" % _port
        _ready = False
        _why = ""
        for _ in range(40):
            time.sleep(0.25)
            try:
                _ur.urlopen(_base + "/api/models", timeout=2).read()
                _ready = True
                break
            except Exception as _e:
                _why = "%s: %s" % (type(_e).__name__, str(_e)[:100])
        if not _ready:
            try:
                _txt = open("/tmp/upscaler-guard-server.log").read()[-300:]
            except Exception:
                _txt = "(no output)"
            check("a server with a spending cap starts", False, "it did not answer (" + _why + ") " + _txt)
        else:
            _req = _ur.Request(
                _base + "/api/run",
                data=json.dumps({
                    "path": src, "backend": "cloud", "cloud_engine": "dloral",
                    "model": "", "scale": 4, "look": "polished",
                    "denoise_engine": "bilateral", "encoder": "h264",
                    "rent": True, "rent_approved": True,
                    "rent_image": "docker.io/bzz1/upscaler:latest"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                _ur.urlopen(_req, timeout=60)
                _body = {}
            except _ue.HTTPError as _e:
                _body = json.loads(_e.read().decode() or "{}")
            _err = (_body.get("error") or "").lower()
            check("a job over the session cap is refused",
                  "cap" in _err, (_body.get("error") or "")[:140])
            check("the refusal names the cap",
                  not _body.get("id"), json.dumps(_body)[:120])
            _sp2 = json.loads(_ur.urlopen(
                _base + "/api/spend", timeout=10).read().decode())
            check("the capped server reports its cap",
                  abs(float(_sp2.get("cap") or 0) - 0.0001) < 1e-9,
                  json.dumps(_sp2)[:120])
    except Exception as e:
        check("the spending cap holds", False, str(e)[:140])
    finally:
        # The server the test started has to go with it. Eight of them
        # were left running before this was noticed.
        try:
            _proc.terminate()
            _proc.wait(timeout=5)
        except Exception:
            pass
        try:
            _logf.close()
        except Exception:
            pass

    # The whole way round: the estimate, the user's approval, the job.
    _, est = call("/api/estimate", {
        "path": src, "backend": "cloud", "cloud_engine": "dloral", "model": "",
        "scale": 4, "look": "polished", "denoise_engine": "bilateral",
        "encoder": "h264", "rent": True,
        "rent_image": "docker.io/bzz1/upscaler:latest"})
    aid = est.get("approval_id")
    check("an estimate comes with an approval to give", bool(aid),
          json.dumps(est)[:120])
    st_noauth, ap_noauth = call("/api/approve", {"id": aid, "yes": True})
    check("an approval without the page's token is refused",
          st_noauth == 403 and ap_noauth.get("state") != "approved",
          json.dumps(ap_noauth)[:120])
    _, ap = call("/api/approve", {"id": aid, "yes": True},
                 headers={"X-Approve-Token": page_token()})
    check("the user can approve the price", ap.get("state") == "approved",
          json.dumps(ap)[:120])

    # A request that names its own approval cannot rent. "rent_approval"
    # as a list used to make the gate raise, the gate swallowed it, and the
    # job took "rent_approved": true from the request. The path is one the
    # server refuses (403) AFTER the gate, so even a broken gate rents
    # nothing here; a working one answers 400 before it gets that far.
    st_by, by = call("/api/run", {
        "path": "/nonexistent/clip.mp4", "backend": "cloud",
        "cloud_engine": "seedvr2", "rent": True, "rent_approval": [1],
        "rent_approved": True, "rent_estimate": {"gpu": "x"}})
    check("a job cannot approve itself by malforming the approval",
          st_by == 400 and not by.get("id"), f"{st_by} {json.dumps(by)[:100]}")

    # A web page in the user's browser must not be able to drive this
    # server: a cross-site POST carries its own Origin, and a rebound DNS
    # name carries its own Host.
    st_o, _ = call("/api/estimate", {"path": src}, headers={"Origin": "http://evil.example"})
    check("a request from another site is refused", st_o == 403, str(st_o))
    st_h, _ = call("/api/spend", headers={"Host": "evil.example:8790"})
    check("a request for another host name is refused", st_h == 403, str(st_h))
    status, job = call("/api/run", {
        "path": src, "backend": "cloud", "cloud_engine": "dloral", "model": "",
        "scale": 4, "look": "polished", "denoise_engine": "bilateral",
        "encoder": "h264", "rent": True, "rent_approval": aid,
        "rent_image": "docker.io/bzz1/upscaler:latest"})
    jid = job.get("id")
    check("the job is accepted (the scale check no longer refuses it)",
          bool(jid), json.dumps(job)[:160])
    if jid:
        time.sleep(3)
        status, j = call(f"/api/job?id={jid}")
        err = (j.get("error") or "")
        check("the job refuses", j.get("state") in ("error", "failed"), j.get("state"))
        check("the refusal names the engine", "dloral" in err, err[:120])
        check("the refusal says what to do instead",
              "BUILD_DLORAL" in err or "Polished" in err, err[:120])
        check("the refusal came before the denoise",
              not any("de-noised" in (l or "") for l in (j.get("log") or [])))

    # 3b. the DLoRAL canvas: the model works from a small canvas and its
    #     own 4x lands the result on the target. Without this the whole
    #     source went up and the result came back four times as big.
    try:
        import engine
        w, h = engine.canvas_for((1920, 1080), 1920, 1080, 4)
        check("the DLoRAL canvas is a quarter of the target",
              (w, h) == (480, 270), f"{w}x{h}")
        w2, h2 = engine.canvas_for((1920, 1080), 1920, 1080, 4)
        check("the canvas is the same every time", (w2, h2) == (w, h))
        # The canvas has to be even (H.264 cannot carry an odd one), so a
        # target that is not a multiple of the scale comes back a couple of
        # pixels short. The finishing pass scales it to the target, so the
        # file is the size that was asked for.
        w3, h3 = engine.canvas_for((400, 300), 400, 300, 4)
        check("the canvas is an even number of pixels",
              w3 % 2 == 0 and h3 % 2 == 0, f"{w3}x{h3}")
        check("the canvas lands on the target, or a hair under it",
              abs(w3 * 4 - 400) <= 4 and abs(h3 * 4 - 300) <= 4,
              f"{w3*4}x{h3*4} against 400x300")
    except Exception as e:
        check("the canvas rule exists", False, str(e)[:120])

    # 3b2. the DLoRAL job must collect the frames the script MADE, not the
    #      ones it read. The script writes both: the input frames next to
    #      the video it read, and the diffusion result in its output
    #      directory. Picking up the wrong set gives back the canvas and
    #      throws the whole diffusion away - and it looks like it worked.
    try:
        import sys
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "worker"))
        import dloral_job
        import tempfile
        from PIL import Image
        tmp = tempfile.mkdtemp(prefix="dloral-frames-")
        stem = "piece-00000"
        # the canvas the script was given, and the frames it read
        os.makedirs(os.path.join(tmp, "in", stem))
        Image.new("RGB", (100, 74)).save(
            os.path.join(tmp, "in", stem, "frame_0000.png"))
        # the frames it made: four times the canvas
        os.makedirs(os.path.join(tmp, "out", stem))
        for i in range(1, 4):
            Image.new("RGB", (400, 296)).save(
                os.path.join(tmp, "out", stem, f"frame_{i:04d}.png"))
        made = dloral_job.made_frames(os.path.join(tmp, "out"), stem)
        check("the job finds the frames the script made", len(made) == 3,
              f"{len(made)} frames")
        if made:
            with Image.open(made[0]) as im:
                check("those frames are the diffusion result, not the canvas",
                      im.size == (400, 296), f"{im.size}")
        # The job has to look in ITS OWN output directory - the one it
        # hands the script - and not in the directory the video went into.
        job = dloral_job.Job.__new__(dloral_job.Job)
        job.script_out = os.path.join(tmp, "out")
        got = job._made_frames(stem)
        check("the job looks in the directory it gave the script",
              len(got) == 3, f"{len(got)} frames")
        job.script_out = os.path.join(tmp, "in")
        got2 = job._made_frames(stem)
        check("the frames the script read are not what the job takes",
              len(got2) == 1 and len(got) == 3,
              f"{len(got2)} read against {len(got)} made")
    except Exception as e:
        check("the frame collection rule exists", False, str(e)[:140])

    # 3c2. a background job must not read the file the caller may delete.
    #      The worker hands the upload over in a temporary directory and
    #      cleans it up the moment it answers with the job id, so a job
    #      that reads the original finds nothing - and STAR stopped with
    #      "stack expects a non-empty TensorList".
    try:
        import sys
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "worker"))
        import star_job
        import tempfile
        tmp = tempfile.mkdtemp(prefix="star-lifetime-")
        src = os.path.join(tmp, "in.mp4")
        with open(src, "wb") as fh:
            fh.write(b"not really a video, but it has to exist")
        job = star_job.Job(src, prompt="x")
        # The real start(), with the model run stubbed out: the copy is
        # the thing being checked, not the inference.
        job._run_job = lambda: None
        job.start()
        os.remove(src)
        check("the job has its own copy of the upload",
              os.path.exists(job.video), job.video)
        check("the copy survives the caller deleting the original",
              os.path.getsize(job.video) > 0)
    except Exception as e:
        check("the job keeps its own copy of the upload", False, str(e)[:140])

    # 3c3. the card lifecycle: a warm card is stopped, not terminated.
    #      Terminating throws the disk away with the weights on it, which is
    #      the whole reason a run downloads eleven gigabytes every time.
    try:
        import rental
        calls = []

        class FakePod(rental.RunPod):
            def _request(self, method, path, body=None):
                calls.append((method, path, body))
                return {"id": "fake", "cost": 1.09}

        pod = FakePod("k", gpu="NVIDIA L40S", engine="dloral",
                      keep_warm=True)
        pod.pod_id = "fake"
        pod.destroy()
        check("a warm card is stopped, not terminated",
              calls and calls[0][0] == "POST"
              and calls[0][1].endswith("/action")
              and (calls[0][2] or {}).get("action") == "stop",
              str(calls))
        check("the warm card is not terminated",
              not any(c[0] == "DELETE" for c in calls), str(calls))

        calls2 = []
        pod2 = FakePod("k", gpu="NVIDIA L40S", engine="dloral",
                       keep_warm=False)
        pod2._request = lambda m, p, b=None: calls2.append((m, p, b)) or {}
        pod2.pod_id = "fake"
        pod2.destroy()
        check("a card that is not kept warm is terminated",
              calls2 and calls2[0][0] == "DELETE", str(calls2))
    except Exception as e:
        check("the card lifecycle keeps the weights when it can", False,
              str(e)[:140])

    # 3c4. a DELETE that answers 200 is not proof. Runpod has left a pod
    #       running and billing after one, so the pod is looked up again,
    #       the DELETE repeated, and a pod that will not go is kept in the
    #       pod file for the next start to sweep.
    try:
        import rental
        import tempfile as _tf
        old_state = rental.POD_STATE
        rental.POD_STATE = os.path.join(_tf.mkdtemp(prefix="pods-"), "pods.json")
        try:
            seen = []
            gets = {"n": 0}

            class Stubborn(rental.RunPod):
                def _request(self, method, path, body=None):
                    seen.append(method)
                    if method == "GET":
                        gets["n"] += 1
                        if gets["n"] < 3:
                            return {"id": "p1", "status": "RUNNING"}
                        raise rental.RentalError("Runpod said 404: not found")
                    return {}

            p = Stubborn("k", log=lambda m: None)
            p.pod_id = "p1"
            rental.remember_pod("p1")
            gone = p.terminate(verify_tries=5, verify_wait=0)
            check("a pod still there after its DELETE is deleted again",
                  gone and seen.count("DELETE") == 3, str(seen))
            check("a pod that is really gone leaves the pod file",
                  "p1" not in rental._read_pod_state())

            class Immortal(rental.RunPod):
                def _request(self, method, path, body=None):
                    return {"id": "p2", "status": "RUNNING"} if method == "GET" else {}

            q = Immortal("k", log=lambda m: None)
            q.pod_id = "p2"
            rental.remember_pod("p2")
            check("a pod that will not go is reported, not forgotten",
                  q.terminate(verify_tries=2, verify_wait=0) is False
                  and "p2" in rental._read_pod_state())
        finally:
            rental.POD_STATE = old_state

        r = rental.RunPod("k", log=lambda m: None)
        r.cost_per_hour = 1.0
        r.billing_since = time.time() - 3600
        check("a rental knows what it has cost so far",
              1.0 <= r.cost_so_far() <= 1.02, f"${r.cost_so_far():.4f}")
    except Exception as e:
        check("the termination is verified", False, str(e)[:140])

    # 3c5. Stop has to stop a rented job, and so does the session budget.
    #       Stop used to set a flag nothing on the cloud path read: the card
    #       kept working, and billing, until the whole job was done.
    try:
        import cloud as cloud_mod
        import engine as engine_mod
        tmp5 = tempfile.mkdtemp(prefix="stop-")
        clip = os.path.join(tmp5, "clip.mp4")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc=size=64x48:rate=25", "-frames:v", "120",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", clip],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        sent = []
        real_upscale = cloud_mod.upscale
        cloud_mod.upscale = lambda *a, **k: sent.append(1)
        try:
            cloud_mod.upscale_chunked("http://127.0.0.1:9", clip,
                                      os.path.join(tmp5, "ai.mp4"),
                                      chunk_frames=50, log=lambda m: None,
                                      should_stop=lambda: "cancelled")
            check("Stop stops a rented job before the next piece", False,
                  "it carried on")
        except RuntimeError as e:
            check("Stop stops a rented job before the next piece",
                  "cancelled" in str(e) and not sent, f"{len(sent)} pieces sent")
        finally:
            cloud_mod.upscale = real_upscale

        # The resume: a second run that asks for the same thing pays only
        # for the pieces the first one did not finish.
        sent2 = []

        def _fake(endpoint, part, answer, **k):
            sent2.append(part)
            shutil.copy(part, answer)
        cloud_mod.upscale = _fake
        try:
            rdir = os.path.join(tmp5, "resume")
            cloud_mod.upscale_chunked("http://127.0.0.1:9", clip,
                                      os.path.join(tmp5, "ai1.mp4"), chunk_frames=50,
                                      log=lambda m: None, parts_dir=rdir)
            first = len(sent2)
            os.remove(os.path.join(rdir, "answer-0000050.mp4"))
            with open(os.path.join(rdir, "answer-0000100.mp4"), "wb") as fh:
                fh.write(b"half a file")         # a piece that died mid-write
            cloud_mod.upscale_chunked("http://127.0.0.1:9", clip,
                                      os.path.join(tmp5, "ai2.mp4"), chunk_frames=50,
                                      log=lambda m: None, parts_dir=rdir)
            check("a retry redoes only the pieces that are missing or broken",
                  first == 3 and len(sent2) - first == 2,
                  f"first run {first} pieces, retry {len(sent2) - first}")
            n2, _, _, _ = cloud_mod.probe_video(os.path.join(tmp5, "ai2.mp4"))
            check("the resumed film has every frame", n2 == 120, str(n2))
            a = engine_mod.resume_dir_for(tmp5, clip, engine="seedvr2", resolution=1080)
            b = engine_mod.resume_dir_for(tmp5, clip, engine="seedvr2", resolution=720)
            check("a retry with other settings does not reuse the pieces", a != b)
        finally:
            cloud_mod.upscale = real_upscale

        class _Card:
            def cost_so_far(self):
                return 1.0
        j = engine_mod.Job("t", clip, os.path.join(tmp5, "o.mp4"), {}, tmp5)
        j._rented = _Card()
        j.budget_usd = 0.5
        why = j._should_stop() or ""
        check("a job that has spent its budget is stopped", "cap" in why, why[:100])
        j.budget_usd = 5.0
        check("a job inside its budget carries on", j._should_stop() is None)
        j.cancel()
        check("a cancelled job is stopped", j._should_stop() == "cancelled")
    except Exception as e:
        check("the rented job can be stopped", False, str(e)[:140])

    # 3c7. de-interlace "auto" has to be able to fire: it reads the field
    #       order, and video_info never used to report one.
    try:
        ilace = os.path.join(tempfile.mkdtemp(prefix="ilace-"), "i.mp4")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc=size=128x96:rate=25", "-frames:v", "10",
                        "-vf", "setfield=tff", "-c:v", "libx264",
                        "-flags", "+ildct+ilme", "-x264opts", "tff=1",
                        "-pix_fmt", "yuv420p", ilace],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        fo = engine_mod.video_info(ilace).get("field_order")
        check("an interlaced source is seen as interlaced",
              fo in ("tt", "bb", "tb", "bt"), repr(fo))
    except Exception as e:
        check("an interlaced source is seen as interlaced", False, str(e)[:140])

    # 3c6. the ledger is settled on what the card cost, not on the guess.
    #       (A scratch ledger: the real one is never touched by the suite.)
    try:
        # By its path: the worker's own server.py shares the name.
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "upscaler_gui_server",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "server.py"))
        server_mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(server_mod)
        old_file, old_spend = server_mod.SPEND_FILE, server_mod.SPEND
        server_mod.SPEND_FILE = os.path.join(tempfile.mkdtemp(prefix="spend-"), "s.json")
        server_mod.SPEND = {"total": 0.40, "jobs": []}
        try:
            entry = {"cost": 0.40, "settled": False}
            server_mod.SPEND["jobs"].append(entry)
            settle = server_mod._settle_spend(entry, 0.40)
            settle(0.65)
            check("a job that ran over its estimate is booked at what it cost",
                  abs(server_mod.SPEND["total"] - 0.65) < 1e-6,
                  f"${server_mod.SPEND['total']:.2f}")
            settle(0.10)
            check("a job is settled once", abs(server_mod.SPEND["total"] - 0.65) < 1e-6)
        finally:
            server_mod.SPEND_FILE, server_mod.SPEND = old_file, old_spend
    except Exception as e:
        check("the ledger is settled", False, str(e)[:140])

    # 3c0. the ffmpeg has to RUN. Homebrew replaces ffmpeg and its
    #       libraries one at a time, and in the middle of that swap the
    #       binary on the PATH cannot load its own libraries any more -
    #       which killed a cloud upscale that had already paid for the
    #       card. The tool picks an ffmpeg that actually starts.
    try:
        import ffbin
        import subprocess as sp
        ff = ffbin.ffmpeg()
        r = sp.run([ff, "-version"], stdout=sp.PIPE, stderr=sp.PIPE,
                   timeout=30)
        check("the ffmpeg we use actually runs", r.returncode == 0, ff)
    except Exception as e:
        check("the ffmpeg we use actually runs", False, str(e)[:140])

    # 3c1. the cards have to EXIST. Runpod's names are not what you
    #       would guess - the A5000 is "NVIDIA RTX A5000" and the 80 GB
    #       A100 is "NVIDIA A100 80GB PCIe" - and a name that does not
    #       exist fails when the card is rented, with a message about
    #       global networking that says nothing about the real problem.
    try:
        import rental as rental_mod
        have = rental_mod.catalogue_ids()
        if have:
            wrong = sorted({c["id"] for cards in rental_mod.GPU_CHOICES.values()
                            for c in cards if c["id"] not in have})
            check("every card we offer exists in the catalogue", not wrong,
                  ", ".join(wrong))
        else:
            # Not checked is not the same as fine.
            print("  skip every card we offer exists in the catalogue - "
                  "the catalogue could not be reached")
        sv = [c["id"] for c in rental_mod.gpu_choices("seedvr2", api_key="",
                                                      out_height=1080)]
        check("a 1080p SeedVR2 job is offered the measured card first, and no A100",
              sv and sv[0] == "NVIDIA L40S"
              and not any("A100" in c or "A5000" in c for c in sv), ", ".join(sv))
    except Exception as e:
        check("every card we offer exists in the catalogue", False,
              str(e)[:140])

    # 3c2. the cards that are offered have to be able to hold the
    #       picture. DLoRAL's VAE decoder runs its attention at the size
    #       of the RESULT, so a 1080p DLoRAL job does not fit on a 24 GB
    #       card - it dies on the card, after the money is spent. A 720p
    #       one does fit, and the cheaper card should be offered for it.
    try:
        import rental as rental_mod
        big = rental_mod.gpu_choices("dloral", out_height=1080)
        small = rental_mod.gpu_choices("dloral", out_height=720)
        check("a 1080p DLoRAL job is not offered a 24 GB card",
              big and all((c.get("vram") or 0) >= 48 for c in big),
              ", ".join(c["id"] for c in big))
        check("a 720p DLoRAL job is offered the cheaper card",
              small and any((c.get("vram") or 0) <= 24 for c in small),
              ", ".join(c["id"] for c in small))
        check("the estimate says how much memory the card needs",
              rental_mod.vram_for("dloral", 1080) == 48
              and rental_mod.vram_for("dloral", 720) == 24
              and rental_mod.vram_for("star", 720) == 48,
              "dloral 1080/720, star 720")
    except Exception as e:
        check("the cards fit the picture", False, str(e)[:140])

    # 3c3. the network volume is off unless somebody asks for it. It
    #       costs $2.80 a month and saves about $0.05 a run, so it only
    #       pays for itself at thirty runs a month - and the code that
    #       keeps a card warm between jobs costs nothing and covers the
    #       same ground for jobs that come one after another.
    try:
        import rental as rental_mod
        check("the network volume is off by default",
              rental_mod.DEFAULT_VOLUME_ID == "",
              repr(rental_mod.DEFAULT_VOLUME_ID))
    except Exception as e:
        check("the network volume is off by default", False, str(e)[:140])

    # 3c4. a centre that has no card right now is asked again. US-TX-3
    #       had an L40S at 10:50 and none at 11:01, and a pod that cannot
    #       be created is not billed - so trying again costs nothing but
    #       a few seconds.
    try:
        import rental as rental_mod

        class FlakyPod(rental_mod.RunPod):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.tries = 0

            def _request(self, method, path, body=None):
                if method == "GET" and path == "/network-volumes":
                    return {"networkVolumes": [
                        {"id": self.volume_id, "dataCenter": "US-TX-3"}]}
                if method == "POST" and path == "/pods":
                    self.tries += 1
                    if self.tries < 3:
                        raise rental_mod.RentalError(
                            "Runpod said 400: There are no longer any "
                            "instances available")
                    return {"id": "fake", "cost": 1.09}
                return {}

        flaky = FlakyPod("k", gpu="NVIDIA L40S", engine="dloral",
                         volume_id="fake-volume")
        flaky.approve({})
        flaky.retry_wait = 0        # the test does not wait
        flaky.create()
        check("a centre without a card is asked again",
              flaky.tries == 3, f"{flaky.tries} tries")
    except Exception as e:
        check("a centre without a card is asked again", False, str(e)[:140])

    # 3d. the DLoRAL encode: the frames the script makes are numbered
    #      from ONE, and the result has to keep the frame rate it came in
    #      at. Both of these were wrong, and both of them failed quietly.
    try:
        import sys
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "worker"))
        import dloral_job
        import tempfile
        from PIL import Image
        tmp = tempfile.mkdtemp(prefix="dloral-encode-")
        fdir = os.path.join(tmp, "frames")
        os.makedirs(fdir)
        for i in (1, 2, 3):        # the script numbers them from one
            Image.new("RGB", (64, 48), (i * 40, 20, 10)).save(
                os.path.join(fdir, f"frame_{i:04d}.png"))
        job = dloral_job.Job.__new__(dloral_job.Job)
        job.out_dir = os.path.join(tmp, "out")
        job.fps = 50.0
        piece = {"name": "piece-00000.mp4", "state": "pending", "bytes": 0}
        job._encode(dloral_job.made_frames(fdir, "frames"), piece, fdir)
        made = os.path.join(job.out_dir, piece["name"])
        check("the encode of frames numbered from one works",
              os.path.exists(made), piece.get("state"))
        if os.path.exists(made):
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                 "-show_entries", "stream=nb_read_frames,avg_frame_rate,width,height",
                 "-of", "default=nw=1", made], stdout=subprocess.PIPE).stdout.decode()
            vals = dict(line.split("=") for line in out.splitlines() if "=" in line)
            check("all the frames are in the video",
                  vals.get("nb_read_frames") == "3", out.replace("\n", " "))
            check("the result keeps the frame rate it came in at",
                  vals.get("avg_frame_rate") == "50/1", out.replace("\n", " "))
    except Exception as e:
        check("the DLoRAL encode works", False, str(e)[:140])

    # 3f. the pieces have to overlap by one frame. The DLoRAL script works
    #      on a two-frame window and makes the second one, so it cannot
    #      make the first frame of the video it is given. Every piece used
    #      to come back one frame short - and a film cut into pieces loses
    #      a frame at every seam and drifts away from its own audio.
    try:
        tmp2 = tempfile.mkdtemp(prefix="dloral-split-")
        clip = os.path.join(tmp2, "clip.mp4")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc=size=64x48:rate=25", "-frames:v", "25",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", clip],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        job2 = dloral_job.Job.__new__(dloral_job.Job)
        job2.video = clip
        job2.in_dir = os.path.join(tmp2, "in")
        job2.out_dir = os.path.join(tmp2, "out")
        job2.pieces = []
        job2.lines = []
        job2._print = lambda m: None
        job2.log = job2._record
        old_piece = dloral_job.PIECE_FRAMES
        dloral_job.PIECE_FRAMES = 10
        try:
            job2._split()
        finally:
            dloral_job.PIECE_FRAMES = old_piece
        counts = []
        for p in job2.pieces:
            out = os.path.join(job2.in_dir, p["name"])
            n = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                 "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1",
                 out], stdout=subprocess.PIPE).stdout.decode()
            try:
                counts.append(int(n.split("=")[1].strip()))
            except Exception:
                counts.append(0)
        # Every piece is cut one frame longer than the frames it has to give
        # back: the extra one is the overlap the script's blind spot eats.
        check("the pieces overlap by one frame",
              counts and all(c == p["frames"] + 1
                             for c, p in zip(counts, job2.pieces)),
              str(counts))
        total = sum(p["frames"] for p in job2.pieces)
        check("no frame of the film is lost at a seam", total == 25, str(total))
    except Exception as e:
        check("the pieces overlap by one frame", False, str(e)[:140])

    # 3g. the finishing pass has to work with the look the GUI starts with.
    #      The "clean" look has no blend, and the soft enlargement was built
    #      anyway and then left dangling: ffmpeg refuses a filter graph with
    #      an unconnected output, so every cloud job with the default look
    #      died at the finishing pass - after the card was paid for.
    try:
        import cloud as cloud_mod
        import engine as engine_mod
        tmp3 = tempfile.mkdtemp(prefix="finishing-")
        ai = os.path.join(tmp3, "ai.mp4")
        original = os.path.join(tmp3, "src.mp4")
        for path in (ai, original):
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                            "testsrc=size=64x48:rate=25", "-frames:v", "4",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", path],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out = os.path.join(tmp3, "out.mp4")
        cloud_mod.finish_locally(ai, original, out, scale=2,
                                 look=dict(engine_mod.LOOKS["clean"]),
                                 encoder="h264", target=(64, 48),
                                 log=lambda m: None)
        check("the finishing pass works with the clean look",
              os.path.exists(out) and os.path.getsize(out) > 0,
              "the clean look has no blend")
    except Exception as e:
        check("the finishing pass works with the clean look", False, str(e)[:140])

    # 3h. the grained looks have to finish on the ffmpeg that is here. The
    #      perlin grain source needs ffmpeg 7.1; on 6.1 (Ubuntu, Debian) the
    #      generator failed silently and the pass died opening a grain file
    #      that was never made - after the card was paid for.
    try:
        for name in ("enhance", "polished"):
            out = os.path.join(tmp3, f"out-{name}.mp4")
            cloud_mod.finish_locally(ai, original, out, scale=2,
                                     look=dict(engine_mod.LOOKS[name]),
                                     encoder="h264", target=(64, 48),
                                     log=lambda m: None)
            check(f"the {name} look finishes on this ffmpeg",
                  os.path.exists(out) and os.path.getsize(out) > 0,
                  "perlin" if __import__("ffbin").has_filter("perlin")
                  else "no perlin here - the static grain stands in")
    except Exception as e:
        check("the grained looks finish on this ffmpeg", False, str(e)[:140])

    # 3i. the grade must not make a dark picture heavier. eq=contrast pivots
    #      on mid-grey, so on this material (median 41) it took the median
    #      down six levels; colorlevels=rimin was a black CRUSH, not a lift.
    #      A ramp from black (16) to a dark grey goes through every look's
    #      grade: the median may not go down and the white must stay white.
    try:
        import numpy as np
        ramp = os.path.join(tmp3, "ramp.mp4")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                        "nullsrc=size=256x64:rate=25,geq=lum='16+X*219/255':cb=128:cr=128",
                        "-frames:v", "2", "-c:v", "libx264", "-qp", "0",
                        "-pix_fmt", "yuv420p", ramp],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

        def luma(vf):
            raw = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", ramp, "-frames:v", "1",
                 "-vf", (vf + "," if vf else "") + "format=yuv420p",
                 "-f", "rawvideo", "-"], stdout=subprocess.PIPE, check=True).stdout
            return np.frombuffer(raw, np.uint8)[:256 * 64].reshape(64, 256)[32]

        base = luma("")
        for name, lk in engine_mod.LOOKS.items():
            vf = ",".join(engine_mod.grade_filters(lk))
            got = luma(vf) if vf else base
            dark = slice(0, 64)          # the bottom quarter: 16 .. ~70
            check(f"the {name} grade does not darken the shadows",
                  int(got[dark].astype(int).sum()) >= int(base[dark].astype(int).sum()),
                  f"mean {base[dark].mean():.1f} -> {got[dark].mean():.1f}")
            check(f"the {name} grade keeps the white",
                  int(got[-1]) >= int(base[-1]) - 2, f"{base[-1]} -> {got[-1]}")
    except Exception as e:
        check("the grade guard runs", False, str(e)[:140])

    # 3e. the local worker: the one that runs on this Mac's GPU. It is a
    #      separate process from the GUI, and if it is not there the local
    #      presets are dead ends.
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:8791/ping")
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode()
        check("the local worker answers", '"ok"' in body, body[:80])
    except Exception as e:
        check("the local worker answers", False, str(e)[:80])

    # 3f. every engine has to be able to price itself without an error.
    if os.path.exists(src):
        for eng in ("realesrgan", "seedvr2", "dloral", "star"):
            st, e = call("/api/estimate", {"path": src, "backend": "cloud",
                                           "cloud_engine": eng, "model": "",
                                           "scale": 4, "look": "polished",
                                           "rent": True})
            check(f"the {eng} engine can be priced",
                  st == 200 and not e.get("error") and (e.get("one_clip_cost") or 0) > 0,
                  json.dumps(e)[:120])

    # 3g. the space guard: the scratch space has to be somewhere, and the
    #      guard has to be able to say what the disk is doing.
    try:
        import sys
        sys.path.insert(0, os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        from tools import space_guard
        st = space_guard.status()
        check("the scratch space is there", os.path.isdir(space_guard.SCRATCH),
              space_guard.SCRATCH)
        check("the guard can see the free space", st["free_bytes"] is not None)
        # iCloud Drive only exists on a Mac. Anywhere else (a cloud sandbox,
        # a Linux box) there is nothing to check, and a FAIL would only
        # teach the reader to ignore the suite.
        if sys.platform == "darwin":
            check("the iCloud Drive is there for when it is needed",
                  st["ic_cloud"] is not None, str(st["ic_cloud"]))
        else:
            print("  skip the iCloud Drive check - this is not a Mac")
        check("the guard does nothing while there is space",
              space_guard.ensure_space(min_free_gib=1) is None)
    except Exception as e:
        check("the space guard exists", False, str(e)[:140])

    # 3c. the image guard itself: the default image for DLoRAL has the
    #     diffusion layer, an image without it is refused.
    try:
        import engine
        import rental
        engine.check_image_can_run(rental.DEFAULT_IMAGE_DLORAL, "dloral")
        check("the default image can run DLoRAL", True)
        try:
            engine.check_image_can_run("docker.io/bzz1/upscaler:latest", "dloral")
            check("an image without DLoRAL is refused", False, "it was allowed")
        except Exception as e:
            check("an image without DLoRAL is refused", "dloral" in str(e))
    except Exception as e:
        check("the image guard exists", False, str(e)[:120])
    time.sleep(2)
    after = pods()
    if before is not None and after is not None:
        check("nothing was rented", len(after) <= len(before),
              f"{len(before)} -> {len(after)} pods")

    print()
    if FAILED:
        print(f"{len(FAILED)} check(s) failed: " + ", ".join(FAILED))
        return 1
    print("all the guards hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
