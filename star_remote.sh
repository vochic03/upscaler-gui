#!/bin/sh
# STAR on a machine that actually has the GPU it needs.
#
#   ./star_remote.sh pack  <source.mp4> <clip.mp4> [start_s] [seconds] [width]
#       Cut the segment you want and shrink it to what STAR can handle.
#       Runs anywhere with ffmpeg - do this on your Mac.
#
#   ./star_remote.sh run   <clip.mp4> <prompt> [output_dir]
#       The part that needs the GPU. Run it on a rented NVIDIA box
#       (RunPod, Lambda, Vast.ai, or a Colab notebook with an L4/A100).
#
# The GPU box needs: an NVIDIA card with 24 GB+ (40 GB is safer), CUDA 11.8+,
# conda, and around 30 GB of disk for the weights.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="${STAR_REPO:-$HOME/STAR}"
WEIGHTS_DIR="${STAR_WEIGHTS:-$HOME/.cache/star-weights}"

case "$1" in
  pack)
    SRC="$2"; CLIP="$3"; START="${4:-0}"; SECS="${5:-2}"; W="${6:-320}"
    echo "cutting ${SECS}s from ${START}s and scaling to ${W}px wide…"
    ffmpeg -y -v error -ss "$START" -t "$SECS" -i "$SRC" \
      -vf "scale=${W}:-2:flags=lanczos,fps=25" \
      -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p -an "$CLIP"
    echo "clip ready: $CLIP ($(du -h "$CLIP" | cut -f1))"
    ;;

  run)
    CLIP="$2"; PROMPT="$3"; OUT="${4:-$HOME/star-out}"
    if ! command -v nvidia-smi >/dev/null; then
      echo "no nvidia-smi here: this has to run on a Linux box with an NVIDIA GPU" >&2
      exit 1
    fi
    echo "GPU:"; nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

    if [ ! -d "$REPO_DIR" ]; then
      echo "cloning STAR…"
      git clone --depth 1 https://github.com/NJU-PCALab/STAR.git "$REPO_DIR"
    fi
    cd "$REPO_DIR"

    if [ ! -d "$WEIGHTS_DIR" ] || [ ! -f "$WEIGHTS_DIR/model.pt" ]; then
      echo "downloading the STAR weights (about 10 GB, once)…"
      mkdir -p "$WEIGHTS_DIR"
      python3 - <<PY
import os, shutil
from huggingface_hub import hf_hub_download
cached = hf_hub_download(repo_id="SherryX/STAR", filename="I2VGen-XL-based/heavy_deg.pt")
shutil.copy(cached, os.path.join("$WEIGHTS_DIR", "model.pt"))
print("weights ready")
PY
    fi

    mkdir -p input/video input/text results
    cp "$CLIP" input/video/clip.mp4
    printf '%s\n' "$PROMPT" > input/text/prompt.txt

    echo "running inference…"
    python3 video_super_resolution/scripts/inference_sr.py \
      --solver_mode fast --steps 15 \
      --input_path input/video/clip.mp4 \
      --model_path "$WEIGHTS_DIR/model.pt" \
      --prompt "$PROMPT" \
      --upscale 4 --max_chunk_len 24 \
      --file_name clip-upscaled.mp4 \
      --save_dir "$OUT"

    echo
    echo "result: $OUT/clip-upscaled.mp4"
    ;;

  *)
    echo "usage:" >&2
    echo "  $0 pack <source.mp4> <clip.mp4> [start_s] [seconds] [width]" >&2
    echo "  $0 run  <clip.mp4> <prompt> [output_dir]" >&2
    exit 1
    ;;
esac
