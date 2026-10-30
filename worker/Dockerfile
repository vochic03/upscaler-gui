# The GPU worker, packaged for a rented NVIDIA box.
#
#   docker build -t drm-upscaler .
#   docker run --gpus all -p 8000:8000 drm-upscaler
#
# On RunPod / Vast.ai you start it as a container template; on DeepInfra as a
# container rental; on fal as a custom deployment. The command is the same
# everywhere, because the worker speaks a plain HTTP protocol (see
# server.py) rather than anything vendor-specific.
#
# The image needs the NVIDIA container runtime so the driver is injected, and
# the Vulkan loader so the NCNN binary can find the GPU.

FROM nvidia/cuda:12.1.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV UPSCALER_BIN=/app/bin/realesrgan-ncnn-vulkan
ENV UPSCALER_MODELS=/app/models
ENV PORT=8000

# ffmpeg for the frame extraction and the encode, python3 for the worker,
# and the Vulkan loader for the NCNN binary.
# No Mesa in here on purpose. Mesa's lavapipe is a software Vulkan renderer,
# and if it is present the NCNN binary picks it instead of the NVIDIA card
# the rental provider gave us - the upscale then runs on the CPU, a hundred
# times slower, and every request runs past the proxy's time limit.
# The NVIDIA Vulkan ICD is injected by the container runtime.
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip \
        ffmpeg python3 ca-certificates \
        libvulkan1 vulkan-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The upstream release: the Real-ESRGAN binary and the models.
# (222 MB, once, at build time - not at run time.)
RUN mkdir -p /app/bin /app/models && \
    curl -fsSL -o /tmp/u.zip \
      https://github.com/hollowaykeanho/Upscaler/releases/download/0.7.0/upscaler-v0.7.0.zip && \
    cd /tmp && unzip -q u.zip && \
    cp upscaler/bin/linux-amd64 /app/bin/realesrgan-ncnn-vulkan && \
    chmod +x /app/bin/realesrgan-ncnn-vulkan && \
    cp upscaler/models/* /app/models/ && \
    rm -rf /tmp/u.zip /tmp/upscaler

# RIFE, for frame interpolation (smooth motion). Same ncnn-vulkan shape, so
# it needs the Vulkan loader and nothing else. The macOS build is what is
# tested here; the linux one is the same layout.
ARG BUILD_RIFE=1
RUN if [ "$BUILD_RIFE" = "1" ]; then \
        curl -fsSL -o /tmp/r.zip \
          https://github.com/nihui/rife-ncnn-vulkan/releases/download/20221029/rife-ncnn-vulkan-20221029-ubuntu.zip && \
        cd /tmp && unzip -q r.zip && \
        cp rife-ncnn-vulkan-20221029-ubuntu/rife-ncnn-vulkan /app/bin/ && \
        cp -r rife-ncnn-vulkan-20221029-ubuntu/rife* /app/ && \
        chmod +x /app/bin/rife-ncnn-vulkan && \
        rm -rf /tmp/r.zip /tmp/rife-ncnn-vulkan-20221029-ubuntu ; \
    fi

ENV RIFE_DIR=/app
ENV RIFE_BIN=/app/bin/rife-ncnn-vulkan

# ---------------------------------------------------------------------------
# The CUDA path.
#
# The rental boxes hand out a working CUDA stack, but their Vulkan ICD
# refuses to give a device to the ncnn build - vkCreateInstance comes back
# empty and the model says "invalid gpu device". CUDA they do hand out, so
# on a rented box the AI pass runs through PyTorch instead, with the same
# weights (realesrgan_cuda.py). The ncnn binary stays for the laptop.
#
# The cu121 wheels run on any driver from 525 up, which covers every box
# Runpod hands out - the newer ones included, because CUDA is backwards
# compatible.
#
# This layer is deliberately before the code copies: it is the slow, big
# one, and the worker code changes far more often than it does.
ARG BUILD_TORCH=1
RUN if [ "$BUILD_TORCH" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends python3-pip && \
        rm -rf /var/lib/apt/lists/* && \
        pip3 install --no-cache-dir --index-url https://download.pytorch.org/whl/cu121 \
            torch==2.5.1 && \
        pip3 install --no-cache-dir "numpy<2" && \
        echo "torch installed." ; \
    else \
        echo "built without torch (use --build-arg BUILD_TORCH=0 to skip it)" ; \
    fi

# boto3 is only for the serverless path, where a result that is too big to
# come back in the answer is written to a bucket. The runpod SDK is what
# pulls the jobs on a serverless endpoint: the job is not posted to the
# container, it is fetched from RUNPOD_WEBHOOK_GET_JOB. Its own layer, so
# the torch layer above stays cached.
RUN pip3 install --no-cache-dir boto3 runpod && echo "boto3 and the runpod SDK installed."

# The weights come from the upstream releases, the same ones the models
# above came from, so nothing new is trusted.
RUN curl -fsSL -o /app/models/realesr-general-x4v3.pth \
        https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-x4v3.pth && \
    curl -fsSL -o /app/models/realesr-general-wdn-x4v3.pth \
        https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-wdn-x4v3.pth && \
    curl -fsSL -o /app/models/RealESRGAN_x4plus.pth \
        https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth && \
    curl -fsSL -o /app/models/RealESRGAN_x4plus_anime_6B.pth \
        https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth && \
    curl -fsSL -o /app/models/RealESRGAN_x2plus.pth \
        https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth && \
    echo "the PyTorch weights are in."

ENV REALESRGAN_MODELS=/app/models

COPY server.py /app/server.py
COPY arch_models.py /app/arch_models.py
COPY realesrgan_cuda.py /app/realesrgan_cuda.py
COPY serverless.py /app/serverless.py

# Where the frames go while a job runs. A rented card comes with plenty of
# disk; a long clip needs room for the frames as PNGs.
VOLUME ["/tmp"]

# ---------------------------------------------------------------------------
# The diffusion engine, as an opt-in layer:
#
#   docker build --build-arg BUILD_DLORAL=1 -t drm-upscaler-dloral .
#
# DLoRAL (github.com/yjsunnn/DLoRAL, NeurIPS 2025) is a one-step diffusion
# video super-resolution. It needs the older OpenMMLab stack, so it gets its
# own image rather than fighting with the rest.
#
# The weights are NOT in the repository (they are on Google Drive), so mount
# them or copy them in:
#
#   /app/DLoRAL/preset/models/ram_swin_large_14m.pth
#   /app/DLoRAL/preset/models/DAPE.pth
#   /app/DLoRAL/preset/models/checkpoints/model.pkl
# ---------------------------------------------------------------------------
ARG BUILD_DLORAL=0
RUN if [ "$BUILD_DLORAL" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends git && \
        rm -rf /var/lib/apt/lists/* && \
        git clone --depth 1 https://github.com/yjsunnn/DLoRAL.git /app/DLoRAL && \
        cd /app/DLoRAL && \
        pip install --no-cache-dir -r requirements.txt && \
        pip install --no-cache-dir openmim && \
        mim install mmcv-full && \
        pip install --no-cache-dir mmedit && \
        mkdir -p /app/DLoRAL/preset/models/checkpoints && \
        echo "DLoRAL installed. Now put the weights in /app/DLoRAL/preset/models." ; \
    else \
        echo "built without DLoRAL (use --build-arg BUILD_DLORAL=1 for that)" ; \
    fi

ENV DLORAL_REPO=/app/DLoRAL
ENV DLORAL_MODELS=/app/DLoRAL/preset/models

# ---------------------------------------------------------------------------
# SeedVR2 - one-step diffusion video super-resolution.
#
# The official ComfyUI release of ByteDance's SeedVR2. It has a standalone
# CLI, so it plugs into the same /upscale endpoint as everything else.
#
#   docker build --build-arg BUILD_SEEDVR2=1 -t upscaler:seedvr2 .
#
# The model weights come from Hugging Face on first run and land in the HF
# cache, so the first job on a cold box is slower.
# ---------------------------------------------------------------------------
ARG BUILD_SEEDVR2=0
RUN if [ "$BUILD_SEEDVR2" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends git && \
        rm -rf /var/lib/apt/lists/* && \
        git clone --depth 1 https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler.git /app/seedvr2 && \
        cd /app/seedvr2 && \
        pip install --no-cache-dir -r requirements.txt && \
        echo "SeedVR2 installed." ; \
    else \
        echo "built without SeedVR2 (use --build-arg BUILD_SEEDVR2=1 for that)" ; \
    fi

ENV SEEDVR2_REPO=/app/seedvr2

# ---------------------------------------------------------------------------
# STAR - text-prompt-guided video super-resolution.
#
# It rebuilds the missing detail with a text-to-video prior, so the prompt
# decides what gets invented. The weights are on Hugging Face (SherryX/STAR),
# so the image can be built without a manual download.
#
#   docker build --build-arg BUILD_STAR=1 -t upscaler:star .
#
# STAR wants a big card: about 39 GB of VRAM for 4x on 72 frames. Lower
# STAR_CHUNK if the card is smaller.
# ---------------------------------------------------------------------------
ARG BUILD_STAR=0
RUN if [ "$BUILD_STAR" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends git curl && \
        rm -rf /var/lib/apt/lists/* && \
        git clone --depth 1 https://github.com/NJU-PCALab/STAR.git /app/STAR && \
        cd /app/STAR && \
        pip install --no-cache-dir -r requirements.txt && \
        mkdir -p pretrained_weight && \
        curl -L --fail -o pretrained_weight/model.pt \
          "https://huggingface.co/SherryX/STAR/resolve/main/I2VGen-XL-based/light_deg.pt?download=true" && \
        echo "STAR installed." ; \
    else \
        echo "built without STAR (use --build-arg BUILD_STAR=1 for that)" ; \
    fi

ENV STAR_REPO=/app/STAR
ENV STAR_MODEL=/app/STAR/pretrained_weight/model.pt

EXPOSE 8000
CMD ["python3", "/app/server.py"]
