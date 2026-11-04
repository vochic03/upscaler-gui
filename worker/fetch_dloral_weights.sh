#!/bin/sh
# Fetch the DLoRAL weights.
#
# Eleven gigabytes, so they are not baked into the image by default: the
# card fetches them the first time it needs them. A rented box has the
# bandwidth and the disk for it, and the image stays small.
#
# The project page sends people to Google Drive, which a build cannot fetch
# from. The same files are mirrored on Hugging Face.
set -e

M=/app/DLoRAL/preset/models
SD=/app/DLoRAL/preset_models/stable-diffusion-2-1-base
H=https://huggingface.co/ighoshsubho/DLoraL-Upscaler-Models/resolve/main
B=https://huggingface.co/yujingsun/stable-diffusion-2-1-base/resolve/main

mkdir -p "$M/checkpoints"

get() {
    # get <url> <file> ; skip it if it is already there
    if [ -s "$2" ]; then
        echo "  have $(basename "$2")"
        return 0
    fi
    echo "  fetching $(basename "$2")"
    # A five gigabyte download over HTTP/2 gets its stream cancelled by the
    # server now and then ("HTTP/2 stream 0 was not closed cleanly"), and
    # the whole job dies with it. HTTP/1.1 does not do that, the retry
    # picks up where it stopped, and the speed limit turns a stalled
    # transfer into a retry instead of a hang.
    curl -fsSL --http1.1 \
         --retry 8 --retry-delay 5 --retry-all-errors \
         --speed-limit 10240 --speed-time 120 \
         -C - -o "$2" "$1"
}

get "$H/DAPE.pth" "$M/DAPE.pth"
get "$H/ram_swin_large_14m.pth" "$M/ram_swin_large_14m.pth"
get "$H/model.pkl" "$M/checkpoints/model.pkl"

mkdir -p "$SD/feature_extractor" "$SD/scheduler" "$SD/text_encoder" \
         "$SD/tokenizer" "$SD/unet" "$SD/vae"
get "$B/feature_extractor/preprocessor_config.json" "$SD/feature_extractor/preprocessor_config.json"
get "$B/scheduler/scheduler_config.json" "$SD/scheduler/scheduler_config.json"
get "$B/text_encoder/config.json" "$SD/text_encoder/config.json"
get "$B/text_encoder/model.fp16.safetensors" "$SD/text_encoder/model.safetensors"
get "$B/tokenizer/merges.txt" "$SD/tokenizer/merges.txt"
get "$B/tokenizer/special_tokens_map.json" "$SD/tokenizer/special_tokens_map.json"
get "$B/tokenizer/tokenizer_config.json" "$SD/tokenizer/tokenizer_config.json"
get "$B/tokenizer/vocab.json" "$SD/tokenizer/vocab.json"
get "$B/unet/config.json" "$SD/unet/config.json"
get "$B/unet/diffusion_pytorch_model.fp16.safetensors" "$SD/unet/diffusion_pytorch_model.safetensors"
get "$B/vae/config.json" "$SD/vae/config.json"
get "$B/vae/diffusion_pytorch_model.fp16.safetensors" "$SD/vae/diffusion_pytorch_model.safetensors"

if [ ! -s "$SD/model_index.json" ]; then
    printf '{"_class_name":"StableDiffusionPipeline","_diffusers_version":"0.25.0","feature_extractor":["diffusers","FeatureExtractor"],"scheduler":["diffusers","DDPMScheduler"],"text_encoder":["transformers","CLIPTextModel"],"tokenizer":["transformers","CLIPTokenizer"],"unet":["diffusers","UNet2DConditionModel"],"vae":["diffusers","AutoencoderKL"]}' > "$SD/model_index.json"
fi

echo "the DLoRAL weights are in."
