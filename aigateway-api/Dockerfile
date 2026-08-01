# syntax=docker/dockerfile:1.7

# Customer-facing build targets:
#   gateway-runtime      Lite
#   gateway-rag          Knowledge on NVIDIA
#   gateway-rag-cpu      Knowledge on CPU / Apple container
#   gateway-vision       Studio gateway on NVIDIA
#   gateway-vision-cpu   Studio gateway on Apple container
#   gateway-full         Full on NVIDIA
#   gateway-full-cpu     Full gateway on Apple container
#   comfyui              NVIDIA ComfyUI sharing gateway-cuda-common layers
FROM python:3.12-slim AS gateway-base

ARG TORCH_VERSION=2.13.0
ARG TORCHVISION_VERSION=0.28.0
ARG TORCHAUDIO_VERSION=2.11.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    HF_HOME=/opt/aigateway/hf-cache

WORKDIR /app

# The admin configuration table reads parameter descriptions from the template
# at runtime.  Keep it in every Gateway image instead of relying on the source
# checkout being mounted into the container.
COPY config.yaml.template /app/config.yaml.template

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends curl

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
    "rag": ("rag",),
    "vision": ("vision",),
    "full": ("rag", "vision"),
}
PROFILE_API_EXTRAS = {
    "runtime": (),
    "rag": ("rag",),
    "vision": ("vision",),
    "full": ("rag", "vision"),
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
            if normalized.lower().startswith("aigateway-core"):
                continue
            if normalized not in seen:
                requirements.append(normalized)
                seen.add(normalized)

    add(core.get("build-system", {}).get("requires", []))
    add(api.get("build-system", {}).get("requires", []))
    add(core["project"].get("dependencies", []))
    add(api["project"].get("dependencies", []))
    for extra in PROFILE_CORE_EXTRAS[profile]:
        add(core["project"].get("optional-dependencies", {}).get(extra, []))
    for extra in PROFILE_API_EXTRAS[profile]:
        add(api["project"].get("optional-dependencies", {}).get(extra, []))
    print("\n".join(requirements))


if __name__ == "__main__":
    main()
PY
RUN chmod +x /usr/local/bin/aigateway-docker-requirements

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=10 \
    CMD curl -fsS http://127.0.0.1:8000/health >/dev/null || exit 1
CMD ["uvicorn", "aigateway_api.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

FROM gateway-base AS gateway-runtime-deps
RUN aigateway-docker-requirements runtime > /tmp/requirements-runtime.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /tmp/requirements-runtime.txt

FROM gateway-runtime-deps AS gateway-runtime
ENV AI_GATEWAY_IMAGE_PROFILE=lite
COPY aigateway-core/src /app/aigateway-core/src
COPY aigateway-api/src /app/aigateway-api/src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps --no-build-isolation /app/aigateway-core /app/aigateway-api

# These are the only two PyTorch installation layers. Every ML target derives
# from one of them, so Docker/registry content addressing can deduplicate them.
FROM gateway-runtime-deps AS gateway-cuda-common
ARG TORCH_VERSION
ARG TORCHVISION_VERSION
ARG TORCHAUDIO_VERSION
ENV AI_GATEWAY_ACCELERATOR=cuda
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
      "torch==${TORCH_VERSION}" \
      "torchvision==${TORCHVISION_VERSION}" \
      "torchaudio==${TORCHAUDIO_VERSION}" \
      --extra-index-url https://download.pytorch.org/whl/cu130

FROM gateway-runtime-deps AS gateway-cpu-common
ARG TORCH_VERSION
ARG TORCHVISION_VERSION
ARG TORCHAUDIO_VERSION
ENV AI_GATEWAY_ACCELERATOR=cpu
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
      "torch==${TORCH_VERSION}" \
      "torchvision==${TORCHVISION_VERSION}" \
      "torchaudio==${TORCHAUDIO_VERSION}" \
      --index-url https://download.pytorch.org/whl/cpu

FROM gateway-cuda-common AS gateway-rag-deps
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends build-essential git nodejs npm
RUN --mount=type=cache,target=/root/.npm npm install -g --force @colbymchenry/codegraph
RUN aigateway-docker-requirements rag > /tmp/requirements-rag.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-build-isolation -r /tmp/requirements-rag.txt

FROM gateway-cpu-common AS gateway-rag-cpu-deps
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends build-essential git nodejs npm
RUN --mount=type=cache,target=/root/.npm npm install -g --force @colbymchenry/codegraph
RUN aigateway-docker-requirements rag > /tmp/requirements-rag.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-build-isolation -r /tmp/requirements-rag.txt

FROM gateway-rag-deps AS gateway-rag
ENV AI_GATEWAY_IMAGE_PROFILE=knowledge
COPY aigateway-core/src /app/aigateway-core/src
COPY aigateway-api/src /app/aigateway-api/src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps --no-build-isolation /app/aigateway-core /app/aigateway-api

FROM gateway-rag-cpu-deps AS gateway-rag-cpu
ENV AI_GATEWAY_IMAGE_PROFILE=knowledge
COPY aigateway-core/src /app/aigateway-core/src
COPY aigateway-api/src /app/aigateway-api/src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps --no-build-isolation /app/aigateway-core /app/aigateway-api

FROM gateway-cuda-common AS gateway-vision-deps
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
      build-essential ffmpeg libgl1 libglib2.0-0 tesseract-ocr \
      tesseract-ocr-chi-sim tesseract-ocr-eng
RUN aigateway-docker-requirements vision > /tmp/requirements-vision.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-build-isolation -r /tmp/requirements-vision.txt

FROM gateway-cpu-common AS gateway-vision-cpu-deps
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
      build-essential ffmpeg libgl1 libglib2.0-0 tesseract-ocr \
      tesseract-ocr-chi-sim tesseract-ocr-eng
RUN aigateway-docker-requirements vision > /tmp/requirements-vision.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-build-isolation -r /tmp/requirements-vision.txt

FROM gateway-vision-deps AS gateway-vision
ENV AI_GATEWAY_IMAGE_PROFILE=studio
COPY aigateway-core/src /app/aigateway-core/src
COPY aigateway-api/src /app/aigateway-api/src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps --no-build-isolation /app/aigateway-core /app/aigateway-api

FROM gateway-vision-cpu-deps AS gateway-vision-cpu
ENV AI_GATEWAY_IMAGE_PROFILE=studio
COPY aigateway-core/src /app/aigateway-core/src
COPY aigateway-api/src /app/aigateway-api/src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps --no-build-isolation /app/aigateway-core /app/aigateway-api

FROM gateway-vision-deps AS gateway-full-deps
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends git nodejs npm
RUN --mount=type=cache,target=/root/.npm npm install -g --force @colbymchenry/codegraph
RUN aigateway-docker-requirements full > /tmp/requirements-full.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-build-isolation -r /tmp/requirements-full.txt

FROM gateway-vision-cpu-deps AS gateway-full-cpu-deps
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends git nodejs npm
RUN --mount=type=cache,target=/root/.npm npm install -g --force @colbymchenry/codegraph
RUN aigateway-docker-requirements full > /tmp/requirements-full.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-build-isolation -r /tmp/requirements-full.txt

FROM gateway-full-deps AS gateway-full
ENV AI_GATEWAY_IMAGE_PROFILE=full
COPY aigateway-core/src /app/aigateway-core/src
COPY aigateway-api/src /app/aigateway-api/src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps --no-build-isolation /app/aigateway-core /app/aigateway-api

FROM gateway-full-cpu-deps AS gateway-full-cpu
ENV AI_GATEWAY_IMAGE_PROFILE=full
COPY aigateway-core/src /app/aigateway-core/src
COPY aigateway-api/src /app/aigateway-api/src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps --no-build-isolation /app/aigateway-core /app/aigateway-api

FROM gateway-cuda-common AS comfyui
ARG COMFYUI_VERSION=v0.28.0
ARG COMFYUI_MANAGER_VERSION=4.2.1
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
      build-essential git libegl1 libgbm1 libx11-6 libxdamage1 libxext6 libxfixes3 libxrandr2
# build-essential 提供 gcc；Qwen-Image 的 Llama 文本编码器在 triton JIT 编译
# RoPE kernel (precompute_freqs_cis) 时需要 C 编译器，否则 comfyui_generation_failed。
# 与 gateway Dockerfile 2026-07-09 的修复同款（见 CLAUDE.md gotcha）。
WORKDIR /opt
RUN git clone --branch "${COMFYUI_VERSION}" --depth 1 \
    https://github.com/Comfy-Org/ComfyUI.git /opt/ComfyUI
WORKDIR /opt/ComfyUI
RUN --mount=type=cache,target=/root/.cache/pip \
    printf 'torch==%s\ntorchvision==%s\ntorchaudio==%s\n' \
      "$TORCH_VERSION" "$TORCHVISION_VERSION" "$TORCHAUDIO_VERSION" \
      > /tmp/comfyui-constraints.txt \
    && pip install --no-build-isolation \
      -r requirements.txt --constraint /tmp/comfyui-constraints.txt \
    && pip install --no-build-isolation \
      "comfyui-manager==${COMFYUI_MANAGER_VERSION}"
COPY scripts/comfyui-entrypoint.sh /usr/local/bin/comfyui-entrypoint
COPY scripts/comfyui-manager-config.ini /opt/comfyui-manager-config.ini
RUN chmod 0755 /usr/local/bin/comfyui-entrypoint \
    && mkdir -p models custom_nodes input output user/default/workflows
EXPOSE 8188
HEALTHCHECK --interval=15s --timeout=5s --retries=10 \
    CMD curl -fsS http://127.0.0.1:8188/system_stats >/dev/null || exit 1
ENTRYPOINT ["/usr/local/bin/comfyui-entrypoint"]

FROM gateway-runtime AS gateway
