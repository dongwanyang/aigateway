FROM python:3.12-slim

# ============================================================
# 系统依赖:OCR(tesseract + 中文/英文)+ 视频(ffmpeg/opencv 运行库)+ curl(healthcheck)
# 几乎不变,放最前确保永久缓存
# ============================================================
RUN apt-get update && apt-get install -y --no-install-recommends \
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
# torch 单独前置层:最大依赖(~1GB)、最稳定,几乎不会变
# CPU-only 版本,避免拉入 CUDA 体积
# ============================================================
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# ============================================================
# 其余全部 Python 依赖一次性安装(从 requirements.txt)
# 合并自原 6 个分散 pip 层,单次依赖解析,缓存命中率最高
# requirements.txt 先于源码 COPY,改业务代码不会触发重装
# ============================================================
COPY aigateway-api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ============================================================
# 预下载 Qwen3-Embedding-0.6B 模型权重(避免首次请求延迟)
# 只依赖 torch+transformers,不依赖业务代码 → 放源码前,改代码不触发重下
# ============================================================
RUN python -c "from transformers import AutoModel, AutoTokenizer; \
    AutoTokenizer.from_pretrained('Qwen/Qwen3-Embedding-0.6B', trust_remote_code=True); \
    AutoModel.from_pretrained('Qwen/Qwen3-Embedding-0.6B', trust_remote_code=True); \
    print('Model cached')"

# ============================================================
# 源码最后 COPY:最高频变动,放最后确保上面所有重量级层缓存命中
# ============================================================
COPY aigateway-core/src/aigateway_core /app/aigateway_core
COPY aigateway-api/src/aigateway_api /app/aigateway_api

EXPOSE 8000

CMD ["uvicorn", "aigateway_api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1"]
