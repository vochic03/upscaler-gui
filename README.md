# upscaler-gui

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
