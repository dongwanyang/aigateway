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
    build-essential \
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
# Patch basicsr 兼容新版 torchvision (>=0.20 移除了 functional_tensor)
# basicsr/data/degradations.py 引用 rgb_to_grayscale, 改用新路径。
# 若已修复则 sed 原样保留(noop)。
# ============================================================
RUN sed -i 's|from torchvision.transforms.functional_tensor import rgb_to_grayscale|from torchvision.transforms.functional import rgb_to_grayscale|' \
    /usr/local/lib/python3.12/site-packages/basicsr/data/degradations.py || true

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
# 预下载 RealESRGAN_x4plus 权重（~64MB，渐进式生成超分用）
# 放到 /app/weights/，由 DraftGeneratorStrategy._super_resolve() 加载。
# 容器内对 github.com SSL 握手不稳定，故构建期下载而非运行时。
# --retry/--max-time: 容器内对 github.com 连接不稳定时自动重试，避免单次抖动
#   导致整个镜像构建失败。下载后校验 SHA256，防止截断/篡改的权重被静默加载
#   （torch.load_state_dict 对形状不匹配的损坏权重可能报错也可能产生垃圾输出）。
# ============================================================
RUN mkdir -p /app/weights && \
    curl -L --retry 3 --retry-delay 5 --max-time 300 \
        -o /app/weights/RealESRGAN_x4plus.pth \
        https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth && \
    echo "4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1  /app/weights/RealESRGAN_x4plus.pth" | sha256sum -c -

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
