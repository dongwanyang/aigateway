# syntax=docker/dockerfile:1.7

# Build targets:
#   gateway-runtime  Core OpenAI-compatible gateway (default, no ML frameworks)
#   gateway-rag      Runtime + local embeddings, RAG, CodeGraph and Qwen weights
#   gateway-vision   Runtime + OCR/video/image processing and RealESRGAN
#   gateway-full     RAG + vision; complete single-node evaluation image
#   gateway-gpu      Runtime + PyTorch only; use as a base for custom GPU images
FROM python:3.12-slim AS gateway-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    HF_HOME=/opt/aigateway/hf-cache

WORKDIR /app

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends curl

# Dependency manifests are copied before application source so normal code edits
# do not invalidate the expensive full/RAG/vision dependency and model layers.
COPY aigateway-core/pyproject.toml /app/aigateway-core/pyproject.toml
COPY aigateway-api/pyproject.toml /app/aigateway-api/pyproject.toml

COPY <<'PY' /usr/local/bin/aigateway-docker-requirements
#!/usr/bin/env python3
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path("/app")
PROFILE_CORE_EXTRAS = {
    "runtime": (),
    "gpu": ("gpu",),
    "rag": ("rag",),
    "vision": ("vision", "gpu"),
    "full": ("rag", "vision", "gpu"),
}
PROFILE_API_EXTRAS = {
    "runtime": (),
    "gpu": ("gpu",),
    "rag": ("rag",),
    "vision": ("vision", "gpu"),
    "full": ("all",),
}


def read_pyproject(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def main() -> None:
    profile = sys.argv[1] if len(sys.argv) > 1 else "runtime"
    if profile not in PROFILE_CORE_EXTRAS:
        raise SystemExit(f"unsupported Docker dependency profile: {profile}")

    core = read_pyproject(ROOT / "aigateway-core" / "pyproject.toml")
    api = read_pyproject(ROOT / "aigateway-api" / "pyproject.toml")
    requirements: list[str] = []
    seen: set[str] = set()

    def add(items: list[str]) -> None:
        for item in items:
            normalized = item.strip()
            if not normalized:
                continue
            # The local package is installed from the copied source later with
            # --no-deps. Pulling it from PyPI would make Docker builds stale and
            # would not reflect the current checkout.
            if normalized.lower().startswith("aigateway-core"):
                continue
            if normalized not in seen:
                requirements.append(normalized)
                seen.add(normalized)

    add(core.get("build-system", {}).get("requires", []))
    add(api.get("build-system", {}).get("requires", []))
    add(core["project"].get("dependencies", []))
    add(api["project"].get("dependencies", []))

    core_optional = core["project"].get("optional-dependencies", {})
    api_optional = api["project"].get("optional-dependencies", {})
    for extra in PROFILE_CORE_EXTRAS[profile]:
        add(core_optional.get(extra, []))
    for extra in PROFILE_API_EXTRAS[profile]:
        add(api_optional.get(extra, []))

    print("\n".join(requirements))


if __name__ == "__main__":
    main()
PY
RUN chmod +x /usr/local/bin/aigateway-docker-requirements

EXPOSE 8000

CMD ["uvicorn", "aigateway_api.main:create_app", \
     "--factory", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1"]

FROM gateway-base AS gateway-runtime-deps
RUN aigateway-docker-requirements runtime > /tmp/requirements-runtime.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /tmp/requirements-runtime.txt

FROM gateway-runtime-deps AS gateway-runtime
ENV AI_GATEWAY_IMAGE_PROFILE=runtime
COPY aigateway-core/src /app/aigateway-core/src
COPY aigateway-api/src /app/aigateway-api/src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps --no-build-isolation /app/aigateway-core /app/aigateway-api

FROM gateway-runtime-deps AS gateway-gpu-deps
ENV AI_GATEWAY_IMAGE_PROFILE=gpu
RUN aigateway-docker-requirements gpu > /tmp/requirements-gpu.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /tmp/requirements-gpu.txt

FROM gateway-gpu-deps AS gateway-gpu
COPY aigateway-core/src /app/aigateway-core/src
COPY aigateway-api/src /app/aigateway-api/src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps --no-build-isolation /app/aigateway-core /app/aigateway-api

FROM gateway-runtime-deps AS gateway-rag-deps
ENV AI_GATEWAY_IMAGE_PROFILE=rag
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends build-essential git nodejs npm
RUN --mount=type=cache,target=/root/.npm \
    npm install -g --force @colbymchenry/codegraph
RUN aigateway-docker-requirements rag > /tmp/requirements-rag.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /tmp/requirements-rag.txt
RUN python -c "from transformers import AutoModel, AutoTokenizer; AutoTokenizer.from_pretrained('Qwen/Qwen3-Embedding-0.6B', trust_remote_code=True); AutoModel.from_pretrained('Qwen/Qwen3-Embedding-0.6B', trust_remote_code=True)"

FROM gateway-rag-deps AS gateway-rag
COPY aigateway-core/src /app/aigateway-core/src
COPY aigateway-api/src /app/aigateway-api/src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps --no-build-isolation /app/aigateway-core /app/aigateway-api

FROM gateway-runtime-deps AS gateway-vision-deps
ENV AI_GATEWAY_IMAGE_PROFILE=vision
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        tesseract-ocr \
        tesseract-ocr-chi-sim \
        tesseract-ocr-eng
RUN aigateway-docker-requirements vision > /tmp/requirements-vision.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /tmp/requirements-vision.txt
# BasicSR 1.4.2 still imports a torchvision module removed in torchvision 0.20.
RUN sed -i 's|from torchvision.transforms.functional_tensor import rgb_to_grayscale|from torchvision.transforms.functional import rgb_to_grayscale|' \
    /usr/local/lib/python3.12/site-packages/basicsr/data/degradations.py
RUN mkdir -p /app/weights \
    && curl -L --retry 3 --retry-delay 5 --max-time 300 \
        -o /app/weights/RealESRGAN_x4plus.pth \
        https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth \
    && echo "4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1  /app/weights/RealESRGAN_x4plus.pth" | sha256sum -c -

FROM gateway-vision-deps AS gateway-vision
COPY aigateway-core/src /app/aigateway-core/src
COPY aigateway-api/src /app/aigateway-api/src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps --no-build-isolation /app/aigateway-core /app/aigateway-api

FROM gateway-runtime-deps AS gateway-full-deps
ENV AI_GATEWAY_IMAGE_PROFILE=full
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
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
        tesseract-ocr-eng
RUN --mount=type=cache,target=/root/.npm \
    npm install -g --force @colbymchenry/codegraph
RUN aigateway-docker-requirements full > /tmp/requirements-full.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /tmp/requirements-full.txt
RUN sed -i 's|from torchvision.transforms.functional_tensor import rgb_to_grayscale|from torchvision.transforms.functional import rgb_to_grayscale|' \
    /usr/local/lib/python3.12/site-packages/basicsr/data/degradations.py
RUN python -c "from transformers import AutoModel, AutoTokenizer; AutoTokenizer.from_pretrained('Qwen/Qwen3-Embedding-0.6B', trust_remote_code=True); AutoModel.from_pretrained('Qwen/Qwen3-Embedding-0.6B', trust_remote_code=True)"
RUN mkdir -p /app/weights \
    && curl -L --retry 3 --retry-delay 5 --max-time 300 \
        -o /app/weights/RealESRGAN_x4plus.pth \
        https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth \
    && echo "4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1  /app/weights/RealESRGAN_x4plus.pth" | sha256sum -c -

FROM gateway-full-deps AS gateway-full
COPY aigateway-core/src /app/aigateway-core/src
COPY aigateway-api/src /app/aigateway-api/src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps --no-build-isolation /app/aigateway-core /app/aigateway-api

FROM gateway-runtime AS gateway
