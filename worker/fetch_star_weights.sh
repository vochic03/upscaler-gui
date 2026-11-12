#!/bin/sh
# Fetch the STAR weights.
#
# Seven and a half gigabytes for the I2VGen-XL checkpoint, so they are
# fetched on the card the first time they are needed rather than baked
# into the image: a rented box has the bandwidth and the disk for it, and
# the image stays small enough to pull quickly.
#
# There are two checkpoints in the repository, "light_deg" and "heavy_deg",
# and they are the same size. The light one is the one the Space uses for
# the quick mode, so it is the one here.
set -e

M=${STAR_WEIGHTS:-/app/STAR/pretrained_weight}
H=https://huggingface.co/SherryX/STAR/resolve/main/I2VGen-XL-based

mkdir -p "$M"

if [ -s "$M/model.pt" ]; then
    echo "  have model.pt"
    exit 0
fi

echo "  fetching the STAR weights (7.6 GB, once)"
# A multi-gigabyte download over HTTP/2 gets its stream cancelled by the
# server now and then, and the whole job dies with it. HTTP/1.1 does not
# do that, the retry picks up where it stopped, and the speed limit turns
# a stalled transfer into a retry instead of a hang.
curl -L --fail --http1.1 \
     --retry 8 --retry-delay 5 --retry-all-errors \
     --speed-limit 10240 --speed-time 120 \
     -C - -o "$M/model.pt" "$H/light_deg.pt"
ls -la "$M/model.pt"
echo "the STAR weights are in."
