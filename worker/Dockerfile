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

# ---------------------------------------------------------------------------
# The face model.
#
# Real-ESRGAN enlarges the picture but does not know what a face is, so the
# face stays soft - and in a close-up the face is the only thing in the
# frame. RestoreFormer (TencentARC/GFPGAN, v1.3.4) is a masked transformer
# that repairs faces while keeping more of the original than GFPGAN v1.4
# does, which is what a film-like look needs.
#
# facexlib brings the detector (RetinaFace) and the alignment; the
# architecture itself is vendored in restoreformer_arch.py, because the
# package version of it wants basicsr's registry and this worker does not
# want a registry.
#
# facexlib is installed with --no-deps on purpose: it asks for a newer torch,
# and pip would answer by replacing the pinned cu121 torch with a 550 MB
# wheel that does not match the CUDA the container runtime injects. What it
# actually imports is torchvision, opencv and Pillow, so those are pinned
# instead.
#
# The facexlib weights are fetched here, at build time, so the first job on
# a cold box does not wait for a download.
# ---------------------------------------------------------------------------
ARG BUILD_FACE=1
RUN if [ "$BUILD_FACE" = "1" ]; then \
        pip3 install --no-cache-dir --no-deps facexlib && \
        pip3 install --no-cache-dir "torchvision==0.20.1" \
            "opencv-python-headless==4.10.0.84" Pillow tqdm && \
        pip3 install --no-cache-dir "numpy<2" && \
        curl -fsSL -o /app/models/RestoreFormer.pth \
            https://github.com/TencentARC/GFPGAN/releases/download/v1.3.4/RestoreFormer.pth && \
        mkdir -p /app/facexlib && \
        python3 -c "\
from facexlib.detection import init_detection_model; \
from facexlib.parsing import init_parsing_model; \
init_detection_model('retinaface_resnet50', device='cpu', model_rootpath='/app/facexlib'); \
init_parsing_model('parsenet', device='cpu', model_rootpath='/app/facexlib'); \
print('the face models are in.')" && \
        echo "the face stack installed." ; \
    else \
        echo "built without the face stack (use --build-arg BUILD_FACE=0 to skip it)" ; \
    fi

ENV FACE_MODEL=/app/models/RestoreFormer.pth
ENV FACEXLIB_WEIGHTS=/app/facexlib

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
        apt-get update && apt-get install -y --no-install-recommends git curl python3.10-venv && \
        rm -rf /var/lib/apt/lists/* && \
        git clone --depth 1 https://github.com/yjsunnn/DLoRAL.git /app/DLoRAL && \
        python3 -m venv /app/venvs/dloral && \
        /app/venvs/dloral/bin/pip install --no-cache-dir --upgrade pip setuptools wheel && \
        /app/venvs/dloral/bin/pip install --no-cache-dir \
            torch==2.0.1 torchvision==0.15.2 numpy==1.24.4 \
            diffusers==0.25.0 transformers==4.28.1 peft==0.9.0 accelerate==0.21.0 \
            huggingface_hub==0.21.4 safetensors==0.4.3 \
            timm==0.6.13 einops==0.7.0 scipy==1.10.1 fairscale==0.4.13 loralib==0.1.2 \
            opencv-python-headless==4.8.1.78 PyYAML==6.0.1 tqdm==4.66.1 requests==2.31.0 && \
        /app/venvs/dloral/bin/pip install --no-cache-dir \
            "numpy==1.24.4" "matplotlib==3.7.5" addict yapf termcolor rich prettytable && \
        /app/venvs/dloral/bin/pip install --no-cache-dir --no-deps mmengine==0.7.4 && \
        /app/venvs/dloral/bin/pip install --no-cache-dir --no-deps \
            mmcv-full==1.7.2 \
            -f https://download.openmmlab.com/mmcv/dist/cu117/torch2.0.0/index.html && \
        echo "the DLoRAL stack is in." ; \
    else \
        echo "built without DLoRAL (use --build-arg BUILD_DLORAL=1 for that)" ; \
    fi

ENV DLORAL_REPO=/app/DLoRAL
ENV DLORAL_MODELS=/app/DLoRAL/preset/models

# The code. It comes after the heavy layers so that a change in
# the worker does not throw away the torch and the venv.
ARG BUILD_SEEDVR2=0
RUN if [ "$BUILD_SEEDVR2" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends git \
            libgl1 libglib2.0-0 && \
        rm -rf /var/lib/apt/lists/* && \
        git clone --depth 1 https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler.git /app/seedvr2 && \
        pip install --no-cache-dir einops "omegaconf>=2.3.0" gguf \
            "rotary_embedding_torch>=0.5.3" psutil "opencv-python-headless>=4.8.1.78" && \
        pip install --no-cache-dir "diffusers==0.35.1" "transformers==4.55.2" \
            "peft==0.17.1" "accelerate==1.10.1" && \
        echo "SeedVR2 installed." ; \
    else \
        echo "built without SeedVR2 (use --build-arg BUILD_SEEDVR2=1 for that)" ; \
    fi

# ---------------------------------------------------------------------------
# STAR, the steerable diffusion.
#
#   docker build --build-arg BUILD_STAR=1 -t upscaler:star .
#
# STAR wants a big card: about 39 GB of VRAM for 4x on 72 frames. Lower
# STAR_CHUNK if the card is smaller.
#
# It gets its own venv, for the same reason DLoRAL does: its requirements
# ask for torch 2.0.1, and installing that over the image's torch 2.5.1
# would take the Real-ESRGAN, SeedVR2 and DLoRAL layers down with it.
#
# The weights (7.6 GB) are fetched on the card, not baked in - see
# fetch_star_weights.sh.
# ---------------------------------------------------------------------------
ARG BUILD_STAR=0
RUN if [ "$BUILD_STAR" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends git curl python3.10-venv && \
        rm -rf /var/lib/apt/lists/* && \
        git clone --depth 1 https://github.com/NJU-PCALab/STAR.git /app/STAR && \
        python3 -m venv /app/venvs/star && \
        /app/venvs/star/bin/pip install --no-cache-dir --upgrade pip wheel && \
        /app/venvs/star/bin/pip install --no-cache-dir \
            torch==2.0.1 torchvision==0.15.2 numpy==1.24 \
            xformers==0.0.21 diffusers==0.30.0 open-clip-torch==2.20.0 \
            easydict==1.13 einops==0.8.0 fairscale==0.4.13 torchsde==0.2.6 \
            pytorch-lightning==2.0.1 accelerate opencv-python-headless \
            huggingface_hub==0.23.3 av "numpy==1.24" timm==0.6.13 tqdm==4.66.1 && \
        echo "the STAR stack is in." ; \
    else \
        echo "built without STAR (use --build-arg BUILD_STAR=1 for that)" ; \
    fi

COPY server.py /app/server.py
COPY arch_models.py /app/arch_models.py
COPY realesrgan_cuda.py /app/realesrgan_cuda.py
COPY serverless.py /app/serverless.py
COPY face_restore.py /app/face_restore.py
COPY dloral_job.py /app/dloral_job.py
COPY diffusion_job.py /app/diffusion_job.py
COPY star_job.py /app/star_job.py
COPY restoreformer_arch.py /app/restoreformer_arch.py

# ---------------------------------------------------------------------------
# The weights, and where they come from.
#
# The project page sends people to Google Drive, which a build cannot fetch
# from. The same files are mirrored on Hugging Face, so the image can carry
# them:
#
#   ighoshsubho/DLoraL-Upscaler-Models   DAPE.pth, model.pkl,
#                                        ram_swin_large_14m.pth
#   yujingsun/stable-diffusion-2-1-base  the base model (fp16)
#
# They are eleven gigabytes. Baking them makes the image enormous and the
# push slow, so by default they are fetched on the card instead - a rented
# box has the bandwidth and the disk for it, and it keeps the image small.
# Bake them with --build-arg BUILD_DLORAL_WEIGHTS=1 when the box cannot
# reach the internet.
#
# The base model is fetched component by component, because the code loads
# the tokenizer, the text encoder, the unet, the vae and the scheduler
# separately - there is no pipeline to load.
# ---------------------------------------------------------------------------

COPY fetch_dloral_weights.sh /app/fetch_dloral_weights.sh
COPY fetch_seedvr2_weights.sh /app/fetch_seedvr2_weights.sh
COPY fetch_star_weights.sh /app/fetch_star_weights.sh
RUN chmod +x /app/fetch_dloral_weights.sh /app/fetch_seedvr2_weights.sh \
    /app/fetch_star_weights.sh

ARG BUILD_DLORAL_WEIGHTS=0
RUN if [ "$BUILD_DLORAL_WEIGHTS" = "1" ]; then \
        /app/fetch_dloral_weights.sh && \
        echo "the DLoRAL weights are baked in." ; \
    else \
        echo "the DLoRAL weights are fetched on the card (BUILD_DLORAL_WEIGHTS=1 to bake them)" ; \
    fi

# ---------------------------------------------------------------------------
# The same choice for the other two engines' weights.
#
# Baking them means the pod gets them with the image, from Runpod's own
# CDN, which is five to ten times faster than Hugging Face serves them -
# the image pull runs at 50 MB/s and the weights come at 5-9 MB/s. So a
# baked image trades a bigger pull (and a bigger push, once) for a much
# shorter start.
#
# The SeedVR2 weights are 3.4 GB and the STAR ones 7.6 GB, so those two
# are the ones worth baking. DLoRAL's are eleven.
# ---------------------------------------------------------------------------
ARG BUILD_SEEDVR2_WEIGHTS=0
RUN if [ "$BUILD_SEEDVR2_WEIGHTS" = "1" ]; then \
        /app/fetch_seedvr2_weights.sh && \
        echo "the SeedVR2 weights are baked in." ; \
    else \
        echo "the SeedVR2 weights are fetched on the card (BUILD_SEEDVR2_WEIGHTS=1 to bake them)" ; \
    fi

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
# SeedVR2 (ByteDance, the official ComfyUI release) is the cheap diffusion:
# a 3B transformer instead of the 1.7B UNet DLoRAL runs, an FP8 model that is
# half the size of the FP16 one, and a standalone CLI - no ComfyUI needed.
# Its weights live on Hugging Face, so the card can fetch them.

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
ENV STAR_REPO=/app/STAR
ENV STAR_MODEL=/app/STAR/pretrained_weight/model.pt

ARG BUILD_STAR_WEIGHTS=0
RUN if [ "$BUILD_STAR_WEIGHTS" = "1" ]; then \
        /app/fetch_star_weights.sh && \
        echo "the STAR weights are baked in." ; \
    else \
        echo "the STAR weights are fetched on the card (BUILD_STAR_WEIGHTS=1 to bake them)" ; \
    fi


# ---------------------------------------------------------------------------
# What this image can actually do.
#
# The diffusion engines are opt-in layers, so an image built without them
# cannot run them. The worker used to answer "/health" with all four
# engines no matter what was in it, which sent the job off to fail after the
# card had been rented and the denoise had been done. This list is the
# truth, and it travels with the image.
# ---------------------------------------------------------------------------
RUN printf '{"realesrgan": true' > /app/engines.json && \
    if [ "$BUILD_FACE" = "1" ]; then printf ', "face": true' >> /app/engines.json; fi && \
    if [ "$BUILD_DLORAL" = "1" ]; then printf ', "dloral": true' >> /app/engines.json; fi && \
    if [ "$BUILD_SEEDVR2" = "1" ]; then printf ', "seedvr2": true' >> /app/engines.json; fi && \
    if [ "$BUILD_STAR" = "1" ]; then printf ', "star": true' >> /app/engines.json; fi && \
    printf '}' >> /app/engines.json && \
    cat /app/engines.json && echo

EXPOSE 8000
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# ---------------------------------------------------------------------------
# The DLoRAL stack has to be importable, and it has to have xformers.
#
# mmcv 1.7.2 reaches for pkg_resources, and setuptools removed that in
# version 81. The venv gets the newest setuptools by default, so the import
# fails before the model is even loaded.
#
# And DLoRAL's own inference path asks for memory-efficient attention: the
# UNet gets enable_xformers_memory_efficient_attention(), and its VAE hook
# imports xformers.ops directly. Without xformers the run dies in the
# attention setup - on the card, which is the most expensive place to find
# out. Its own requirements.txt asks for xformers==0.0.20, which is the
# build that goes with torch 2.0.1.
#
# It is a layer of its own, at the end, so that fixing it does not throw
# away the venv and the SeedVR2 install above: both of those take a long
# time to build and neither of them changes when this is fixed.
# ---------------------------------------------------------------------------
RUN if [ "$BUILD_DLORAL" = "1" ]; then \
        /app/venvs/dloral/bin/pip install --no-cache-dir "setuptools==69.5.1" && \
        /app/venvs/dloral/bin/pip install --no-cache-dir "xformers==0.0.20" && \
        /app/venvs/dloral/bin/python -c "import mmcv, mmengine; print('mmcv', mmcv.__version__, 'mmengine', mmengine.__version__)" && \
        /app/venvs/dloral/bin/python -c "import xformers, xformers.ops; print('xformers', xformers.__version__)" && \
        echo "the DLoRAL stack can be imported." ; \
    else \
        echo "no DLoRAL stack to fix up" ; \
    fi

ENTRYPOINT ["/app/entrypoint.sh"]
