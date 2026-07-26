# syntax=docker/dockerfile:1

# Build targets:
#   gateway-runtime  Core OpenAI-compatible gateway (default, no ML frameworks)
#   gateway-rag      Runtime + local embeddings, RAG, CodeGraph and Qwen weights
#   gateway-vision   Runtime + OCR/video/image processing and RealESRGAN
#   gateway-full     RAG + vision; complete single-node evaluation image
#   gateway-gpu      Runtime + PyTorch only; use as a base for custom GPU images
FROM python:3.12-slim AS gateway-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# pyproject.toml is the only Python dependency manifest. Install the local core
# package first so API extras can resolve aigateway-core without PyPI.
COPY aigateway-core/pyproject.toml /app/aigateway-core/pyproject.toml
COPY aigateway-core/src /app/aigateway-core/src
RUN pip install --no-cache-dir /app/aigateway-core

COPY aigateway-api/pyproject.toml /app/aigateway-api/pyproject.toml
COPY aigateway-api/src /app/aigateway-api/src

EXPOSE 8000

CMD ["uvicorn", "aigateway_api.main:create_app", \
     "--factory", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1"]

FROM gateway-base AS gateway-runtime
ENV AI_GATEWAY_IMAGE_PROFILE=runtime
RUN pip install --no-cache-dir /app/aigateway-api

FROM gateway-runtime AS gateway-gpu
ENV AI_GATEWAY_IMAGE_PROFILE=gpu
WORKDIR /app/aigateway-api
RUN pip install --no-cache-dir ".[gpu]"
WORKDIR /app

FROM gateway-runtime AS gateway-rag
ENV AI_GATEWAY_IMAGE_PROFILE=rag
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git nodejs npm \
    && npm install -g --force @colbymchenry/codegraph \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app/aigateway-api
RUN pip install --no-cache-dir ".[rag]"
RUN python -c "from transformers import AutoModel, AutoTokenizer; AutoTokenizer.from_pretrained('Qwen/Qwen3-Embedding-0.6B', trust_remote_code=True); AutoModel.from_pretrained('Qwen/Qwen3-Embedding-0.6B', trust_remote_code=True)"
WORKDIR /app

FROM gateway-runtime AS gateway-vision
ENV AI_GATEWAY_IMAGE_PROFILE=vision
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        tesseract-ocr \
        tesseract-ocr-chi-sim \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app/aigateway-api
RUN pip install --no-cache-dir ".[vision,gpu]"
# BasicSR 1.4.2 still imports a torchvision module removed in torchvision 0.20.
RUN sed -i 's|from torchvision.transforms.functional_tensor import rgb_to_grayscale|from torchvision.transforms.functional import rgb_to_grayscale|' \
    /usr/local/lib/python3.12/site-packages/basicsr/data/degradations.py
RUN mkdir -p /app/weights \
    && curl -L --retry 3 --retry-delay 5 --max-time 300 \
        -o /app/weights/RealESRGAN_x4plus.pth \
        https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth \
    && echo "4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1  /app/weights/RealESRGAN_x4plus.pth" | sha256sum -c -
WORKDIR /app

FROM gateway-runtime AS gateway-full
ENV AI_GATEWAY_IMAGE_PROFILE=full
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ffmpeg \
        git \
        libgl1 \
        libglib2.0-0 \
        nodejs \
        npm \
        tesseract-ocr \
        tesseract-ocr-chi-sim \
        tesseract-ocr-eng \
    && npm install -g --force @colbymchenry/codegraph \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app/aigateway-api
RUN pip install --no-cache-dir ".[all]"
RUN sed -i 's|from torchvision.transforms.functional_tensor import rgb_to_grayscale|from torchvision.transforms.functional import rgb_to_grayscale|' \
    /usr/local/lib/python3.12/site-packages/basicsr/data/degradations.py
RUN python -c "from transformers import AutoModel, AutoTokenizer; AutoTokenizer.from_pretrained('Qwen/Qwen3-Embedding-0.6B', trust_remote_code=True); AutoModel.from_pretrained('Qwen/Qwen3-Embedding-0.6B', trust_remote_code=True)"
RUN mkdir -p /app/weights \
    && curl -L --retry 3 --retry-delay 5 --max-time 300 \
        -o /app/weights/RealESRGAN_x4plus.pth \
        https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth \
    && echo "4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1  /app/weights/RealESRGAN_x4plus.pth" | sha256sum -c -
WORKDIR /app

FROM gateway-runtime AS gateway
