FROM python:3.12-slim

# 安装 curl 用于 healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装基础依赖
RUN pip install --no-cache-dir \
    fastapi uvicorn[standard] gunicorn \
    pyyaml watchdog \
    prometheus-client \
    structlog \
    cachetools lz4 \
    httpx \
    redis hiredis \
    pydantic \
    litellm

# 安装 Embedding 模型依赖（CPU-only PyTorch + transformers）
# 用于 L3 语义缓存的 Qwen/Qwen3-Embedding-0.6B 向量计算
RUN pip install --no-cache-dir \
    torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir \
    "transformers>=4.51.0"

# 预下载 Qwen3-Embedding-0.6B 模型权重（避免首次请求延迟）
RUN python -c "from transformers import AutoModel, AutoTokenizer; AutoTokenizer.from_pretrained('Qwen/Qwen3-Embedding-0.6B', trust_remote_code=True); AutoModel.from_pretrained('Qwen/Qwen3-Embedding-0.6B', trust_remote_code=True); print('Model cached')"

# 复制源码
COPY aigateway-core/src/aigateway_core /app/aigateway_core
COPY aigateway-api/src/aigateway_api /app/aigateway_api

EXPOSE 8000

CMD ["uvicorn", "aigateway_api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1"]
