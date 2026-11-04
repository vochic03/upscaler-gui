#!/bin/sh
# Fetch the SeedVR2 weights.
#
# Four gigabytes for the 3B FP8 model and its VAE, so they are fetched on the
# card the first time they are needed rather than baked into the image: a
# rented box has the bandwidth and the disk for it, and the image stays small
# enough to push quickly.
#
# The FP8 model is half the size of the FP16 one and the same quality to
# look at; the FP16 and 7B models are there for a machine with room.
set -e

M=${SEEDVR2_MODEL_DIR:-/app/seedvr2_models/SEEDVR2}
H=https://huggingface.co/numz/SeedVR2_comfyUI/resolve/main

mkdir -p "$M"

get() {
    # get <url> <file>; skip it if it is already there
    if [ -s "$2" ]; then
        echo "  have $(basename "$2")"
        return 0
    fi
    echo "  fetching $(basename "$2")"
    # A multi-gigabyte download over HTTP/2 gets its stream cancelled by
    # the server now and then ("HTTP/2 stream 0 was not closed cleanly"),
    # and the whole job dies with it. HTTP/1.1 does not do that, the retry
    # picks up where it stopped, and the speed limit turns a stalled
    # transfer into a retry instead of a hang.
    curl -fsSL --http1.1 \
         --retry 8 --retry-delay 5 --retry-all-errors \
         --speed-limit 10240 --speed-time 120 \
         -C - -o "$2" "$1"
}

get "$H/seedvr2_ema_3b_fp8_e4m3fn.safetensors" "$M/seedvr2_ema_3b_fp8_e4m3fn.safetensors"
get "$H/ema_vae_fp16.safetensors" "$M/ema_vae_fp16.safetensors"

echo "the SeedVR2 weights are in."
