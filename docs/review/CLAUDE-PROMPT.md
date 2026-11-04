# The prompt for Claude Code (cloud)

Paste everything under the line into Claude Code on the web, with this
repository connected. It is written for the cloud agent: it works in a
sandbox, it has a large budget, and it is meant to be run in rounds.

---

You have a large budget and a repository that is already finished once. Use
the budget on **work**, not on a summary.

**Your job:** take over `upscaler-gui` - a video upscaler that runs on the
Mac's own GPU or on a rented GPU on Runpod - plus `drm-capture`, a
separate DRM capture tool in the same repository - work out what is actually
wrong with it, and fix it. The goal is a picture that holds up next to a
Topaz reference: detail that looks real, grain that looks like film, and no
"waxy upscale" anywhere.

## Where the work is

**Do not work from `main`.** `main` is the 30 October snapshot and it is
mostly empty of everything that matters. The complete body of work is on:

    branch:  review/complete-work
    pull request:  https://github.com/vochic03/upscaler-gui/pull/1

Branch from `review/complete-work` and open your own pull requests **against
`review/complete-work`**, not against `main`. PR #1 stays the one door
into `main`, and it carries everything, including your work, when it is
merged. Do not merge PR #1 yourself and do not force-push that branch.

## Read in this order

1. `docs/review/WORKLOG.md` - the map. What the tool is, what was decided
   and why, what broke and how it was found, what is still open.
2. `docs/review/FINDINGS.md` - **four things that were measured but
   deliberately NOT changed**, each with the measurement, the reasoning and
   a ready-to-apply diff. They are starting points, not instructions.
   Deducing them yourself and disagreeing with them is a good outcome.
3. `AGENTS.md` - the rules the code lives by. Not decoration: the code
   breaks in ways that are not obvious if you ignore them.
4. `README.md`, `ENGINES.md`, `docs/review/dvd-look-research.md` - the
   tool as it presents itself, the engines and what they cost, and the
   research behind the "DVD look".
5. The code: `engine.py` (the pipeline), `rental.py` (Runpod),
   `cloud.py` (the rented path), `server.py` (the API), `worker/` (what
   runs on the card), `tools/test_cloud_guards.py` (the guards).

The before/after pictures are in `docs/review/images/` - 50 of them, and
they are the evidence. The videos are on the `review-media` release:
`gh release download review-media`. `drm-capture/` is a second tool and
is lower priority - read it, but do not let it crowd out the upscaler.

## Your sandbox is not this Mac

- **Start the GUI first.** It is standard library only, no installs:
  `python3 server.py --port 8790 &`
- **ffmpeg is needed** for the guards and for any measurement:
  `apt-get install -y ffmpeg` if it is missing.
- **One guard will fail and that is correct.** "the local worker answers"
  needs the vendored `upscalayl` binary, which is a macOS build and is
  deliberately not in the repository. Expect exactly that one failure and
  say so; do not chase it.
- **There is no Runpod key in the sandbox.** The guards mock the pod API,
  so they still pass. But you cannot rent anything without one - see the
  rules.
- The GUI binds `127.0.0.1` only. Do not expose it.

## The rules that are not negotiable

- **Never rent a GPU without an estimate stated first and an explicit
  approval.** The user approves the price, not the task: price per hour,
  the spin-up overhead, the projected total. The approval is server-issued
  - an agent can ask for a price but cannot approve its own spending. If
  you want to test something on a card, produce the estimate and stop
  there. The estimates for the specific tests that would help are in
  `FINDINGS.md`.
- **No face restoration.** GFPGAN, RestoreFormer and CodeFormer are in the
  repository as an image layer and all switched off. The user judged
  RestoreFormer over an upscaled video as reading as a retouched face
  rather than as film. Leave it off, do not propose it, do not make it
  easier to turn on. The upscale, the blend and the grain carry the face.
- **Keys live in the macOS Keychain only** - never in a file, a log, a
  commit or a message.
- **Do not merge, tag or release.** Open pull requests; the user merges.
- If a session is about to end, commit and push what you have, and write
  down what is next. Do not leave the branch broken.

## What the budget is for

Work through this in rounds, and do not stop after a plan. A round ends
with a pull request, a passing guard suite and a short note of what changed
and what it fixed.

1. **Verify you can run the guards.** Then read the code and write down
   what you think is wrong, before changing anything.
2. **The finishing grade.** This is free to iterate - no card needed. The
   measurement in `FINDINGS.md` section 2 is the baseline: the `eq=contrast`
   stage drops 3 levels off a median of 41 and pushes 5.4% of the pixels
   onto pure black, and the "polished" and "matte" looks push the same knob
   harder. Measure candidates against the saved AI output and pick the one
   that holds the shadows and gives the midtones back. **Look at the
   pictures**, not just the numbers - the numbers can all be right and the
   picture still be wrong.
3. **The SeedVR2 guide.** `FINDINGS.md` section 1: the guide is a quarter
   of the target, and the model is asked to invent what was thrown away.
   Apply the diff, or a better one. Verifying that it looks better needs
   one 20 s comparison on a card - ask for the estimate first.
4. **The A100 for FP8 weights** (`FINDINGS.md` section 3) and **the missing
   resume** (`FINDINGS.md` section 4). Both are small and both are
   clearly wrong; decide and do.
5. **Then keep going.** You have budget left and the job is bigger than
   four findings. The DVD-look research, the guards, the cost model, the
   card lifecycle, the API-anything surface - all of it is fair game. What
   you find that nobody has mentioned is the most valuable thing you can
   produce.

## The traps this code already knows

`AGENTS.md` has the full list. The ones that will bite you first:

- **Docker:** the base image must be the **amd64** manifest, pulled **by
  digest**. The legacy builder resolves a multi-arch base by the *host*
  platform's manifest, and the arm64 one has no torch 2.5.1. `docker build`
  uploads the whole context at the start, so an edit made while a build runs
  is silently absent from the image.
- **Runpod:** `DELETE /v2/pods/{id}` can answer 200 and still leave the pod
  running and billing. Always re-read `GET /v2/pods` afterwards. The
  capacity check runs *before* the balance check, so a failed create is a
  free availability probe. A pod can only mount a volume from its own data
  centre.
- **The proxy** in front of the pod gives a request about 100 seconds, and
  Cloudflare answers 1010 to a non-browser fingerprint.
- **iCloud Drive is the scratch space**, and it is a pressure valve, not
  spare disk. An evicted file opened **for writing blocks forever** -
  delete it first and let the writer make a new one.
- **The finishing pass** has two silent failure modes that are fixed and
  must stay fixed: a halation that adds arithmetically *subtracts* below
  mid-grey, and ffmpeg's `perlin` grain has a mean of its own that decays
  over the clip, so it has to be measured over the whole file.

## When you are done

Say what you changed, what you rejected and why, what the guards said, and
what would need the user's money or their taste to settle. If something in
the brief turned out to be wrong, say that too.
