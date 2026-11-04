# The prompt for Claude Code

Paste everything under the line into Claude Code, in the repository, and let
it work.

---

You are taking over a completed body of work: **`upscaler-gui`**, a video
upscaler that runs either on this Mac's own GPU or on a rented GPU on
Runpod, plus **`drm-capture`**, a separate DRM capture tool that lives in
the same repository.

Every file that was written during that work is in **one pull request on
one branch**. Nothing was split up, and nothing was left out. Read it,
work out what is actually wrong with it, and decide what to change - then
make the changes and open your own pull request.

---

## 1. Where to start, in this order

1. `docs/review/WORKLOG.md` - what the tool is, what was built, what was
   decided and why, what broke and how it was fixed, what is still open.
   Read this first. It is the map.
2. `docs/review/FINDINGS.md` - four things that were **measured but not
   changed**, each with the measurement, the reasoning and a ready-to-apply
   diff. These are starting points, not instructions. Deducing them yourself
   and disagreeing with them is a perfectly good outcome.
3. `AGENTS.md` - the rules the code lives by. They are not decoration; the
   code breaks in ways that are not obvious if you ignore them.
4. `README.md` and `ENGINES.md` - the tool as it presents itself, and the
   engines and what they cost.
5. The code: `engine.py` (the pipeline), `rental.py` (the GPU side),
   `cloud.py` (the rented path), `server.py` (the API), `worker/` (what
   runs on the card), `tools/test_cloud_guards.py` (the 39 guards).

The before/after pictures are in `docs/review/images/`. The videos are
attached to a release on this repository - `gh release download
review-media` - and listed in `docs/review/MEDIA-MANIFEST.md`.

## 2. What the tool does, in one paragraph

The user has DVDs and SD captures of Serbian television that need to become
sharp, clean 1080p. The tool takes a video and runs it through an
upscaling model - Real-ESRGAN for a faithful enlargement, SeedVR2 or
DLoRAL for diffusion that invents plausible detail, STAR for diffusion
steered by a text prompt - and then finishes it with a grade: denoise, a
blend with a plain lanczos enlargement, sharpening, grain, halation, a
black lift. It runs locally on the Mac's GPU for free, or in the cloud on a
rented card for a few tens of cents.

## 3. The rules that are not negotiable

These are the user's standing decisions, not preferences. Respect them even
if you would have chosen differently.

- **Never rent a GPU without an estimate stated first and an explicit
  approval.** The user approves the price, not the task. The approval is
  server-issued: an agent can ask for an estimate but cannot approve its
  own spending. If you want to test something on a card, produce the
  estimate - price per hour, spin-up overhead, projected total - and stop
  there.
- **No face restoration.** GFPGAN, RestoreFormer and CodeFormer are all in
  the repository as an image layer and all switched off. The user judged
  RestoreFormer over an upscaled video as reading as a retouched face
  rather than as film. Leave it off, and do not propose it. The upscale,
  the blend and the grain carry the face.
- **Keys live in the macOS Keychain only.** Never in a file, a log, a
  commit or a message.
- **The servers bind `127.0.0.1` only.**
- **Do not commit or push without being asked.** If you find something worth
  changing, branch, commit and open a pull request - and say what you
  changed and why.

## 4. The traps this code already knows about

`AGENTS.md` has the full list. The ones that will bite you first:

- **Docker:** the base image must be the **amd64** manifest, pulled **by
  digest**. The legacy builder resolves a multi-arch base by the *host*
  platform's manifest, looked up by digest, and the arm64 one has no torch
  2.5.1. `docker build` uploads the whole context at the start, so an edit
  made while a build runs is silently absent from the image.
- **Runpod:** `DELETE /v2/pods/{id}` can answer 200 and still leave the pod
  running and billing. Always re-read `GET /v2/pods` afterwards. The
  capacity check runs *before* the balance check, so a failed create is a
  free availability probe. A pod can only mount a volume from its own data
  centre.
- **The proxy** in front of the pod gives a request about 100 seconds, and
  Cloudflare answers 1010 to a non-browser fingerprint. Long work has to go
  in pieces.
- **iCloud Drive is the scratch space**, and it is a pressure valve, not
  spare disk. An evicted file that is opened **for writing blocks forever**
  - delete it first and let the writer make a new one.
- **The finishing pass** has two silent failure modes that are already
  fixed and must stay fixed: a halation that adds arithmetically *subtracts*
  below mid-grey, and ffmpeg's `perlin` grain has a mean of its own that
  decays over the clip, so it has to be measured over the whole file.

## 5. What to actually do

1. **Read it all.** The whole pull request, not the diff - this is a body of
   work, and the parts that make sense together are spread across it.
2. **Deducing what is wrong is your job.** Where you disagree with
   `FINDINGS.md`, say so and back it with a measurement or a reading of the
   code. Where you find something nobody has mentioned, that is the most
   valuable thing you can produce.
3. **Decide.** Make the call on the look and on the architecture. Write down
   what you decided and what you rejected, and why.
4. **Change it.** Branch from `main`, commit in reviewable pieces, and open
   **one** pull request that says what changed and what it fixes.
5. **Verify it.** `python3 tools/test_cloud_guards.py` - 39 guards, they
   all hold now and they have to keep holding. Anything you change in the
   finishing can be checked for free against the saved AI output in
   `docs/review/images/` and the release media; you do not need a card to
   measure a grade.
6. **Say what you did not do**, and what would need the user's money or
   their taste to decide.

The point of handing you the whole thing is that you were not here for the
parts that took the longest. Deducing it is the point.
