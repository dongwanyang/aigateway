FROM python:3.12-slim

# ============================================================
# 系统依赖: OCR + 视频 + curl(healthcheck)
# ============================================================
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    curl \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    tesseract-ocr-eng \
    libgl1 \
    libglib2.0-0 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ============================================================
# CPU-only torch — 最重 (~900MB)，几乎不变
# ============================================================
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install torch --index-url https://download.pytorch.org/whl/cpu

# ============================================================
# Python 依赖
# --mount=type=cache,target=/root/.cache/pip 跨构建复用 pip 缓存
# ============================================================
COPY aigateway-api/requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /tmp/requirements.txt

# ============================================================
# 预下载 Qwen3-Embedding-0.6B 模型权重
# 权重存 HuggingFace 缓存（~600MB），用独立 cache mount
# ============================================================
RUN --mount=type=cache,target=/root/.cache/huggingface \
    python -c "from transformers import AutoModel, AutoTokenizer; \
    AutoTokenizer.from_pretrained('Qwen/Qwen3-Embedding-0.6B', trust_remote_code=True); \
    AutoModel.from_pretrained('Qwen/Qwen3-Embedding-0.6B', trust_remote_code=True)"

# ============================================================
# 源码（最高频变动）
# ============================================================
COPY aigateway-core/src/aigateway_core /app/aigateway_core
COPY aigateway-api/src/aigateway_api /app/aigateway_api

EXPOSE 8000

CMD ["uvicorn", "aigateway_api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1"]
