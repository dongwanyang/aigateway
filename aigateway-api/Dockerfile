FROM python:3.12-slim

# 安装 curl 用于 healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装依赖
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

# 复制源码
COPY aigateway-core/src/aigateway_core /app/aigateway_core
COPY aigateway-api/src/aigateway_api /app/aigateway_api

EXPOSE 8000

CMD ["uvicorn", "aigateway_api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1"]
