FROM python:3.12-slim

# ============================================================
# 系统依赖: OCR + 视频 + curl(healthcheck) + git(Code RAG Git 源浅克隆需要 git CLI)
# + nodejs/npm(CodeGraph 官方 CLI 通过 npm 安装)
# 几乎不变，放最前确保永久缓存
# ============================================================
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    nodejs \
    npm \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    tesseract-ocr-eng \
    libgl1 \
    libglib2.0-0 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ============================================================
# CPU-only torch 单独前置层: 最重(~900MB), 几乎不变
# 注: pip install torch --index-url https://download.pytorch.org/whl/cpu
# 在这个环境中 SSL 握手失败(CloudFlare CA 不兼容), 回退到默认 PyPI。
# 默认 PyPI 会装 GPU 版(~1.2GB, 含 CUDA 运行时), 在纯 CPU 机器上自动退化。
# 如果能解决 SSL 问题, 换回 --index-url 镜像可缩小到 ~900MB。
RUN pip install --no-cache-dir torch

# ============================================================
# Python 依赖（从 requirements.txt）
# requirements.txt 先于源码 COPY，改业务代码不会触发重装
# ============================================================
COPY aigateway-api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ============================================================
# CodeGraph 官方 CLI（多语言,SQLite: .codegraph/codegraph.db）
# 走 npm 全局安装,统一使用 README 文档里的 codegraph 命令入口。
# 注意: 这里安装的是 GitHub 项目 @colbymchenry/codegraph,不是同名 Python 包 API。
# --force: legacy builder 复用层时 /usr/local/bin/codegraph 可能已存在,直接覆盖。
# ============================================================
RUN npm install -g --force @colbymchenry/codegraph

# ============================================================
# 预下载 Qwen3-Embedding-0.6B 模型权重（~600MB，独立层）
# ============================================================
RUN python -c "from transformers import AutoModel, AutoTokenizer; \
    AutoTokenizer.from_pretrained('Qwen/Qwen3-Embedding-0.6B', trust_remote_code=True); \
    AutoModel.from_pretrained('Qwen/Qwen3-Embedding-0.6B', trust_remote_code=True)"

# ============================================================
# 源码（最高频变动，放最后）
# ============================================================
COPY aigateway-core/src/aigateway_core /app/aigateway_core
COPY aigateway-api/src/aigateway_api /app/aigateway_api

EXPOSE 8000

CMD ["uvicorn", "aigateway_api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1"]
