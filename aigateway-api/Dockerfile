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

# 安装 Embedding 模型依赖（CPU-only PyTorch + sentence-transformers）
# 注意：首次构建耗时较长（下载 ~800MB torch CPU + ~200MB sentence-transformers）
# 如在 CI/CD 中构建，建议使用缓存层
# 取消以下注释以启用本地 Qwen3-Embedding-0.6B 模型（推荐生产环境使用）：
# RUN pip install --no-cache-dir \
#     torch --index-url https://download.pytorch.org/whl/cpu \
#     && pip install --no-cache-dir \
#     "sentence-transformers>=2.7.0" \
#     "transformers>=4.51.0"
# RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('Qwen/Qwen3-Embedding-0.6B')"

# 复制源码
COPY aigateway-core/src/aigateway_core /app/aigateway_core
COPY aigateway-api/src/aigateway_api /app/aigateway_api

EXPOSE 8000

CMD ["uvicorn", "aigateway_api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1"]
