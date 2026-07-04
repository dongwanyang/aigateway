FROM python:3.12-slim

# 安装系统依赖（OCR tesseract + 视频 ffmpeg/opencv + curl healthcheck）
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
    litellm \
    Pillow

# 安装多模态处理依赖
RUN pip install --no-cache-dir \
    pytesseract \
    opencv-python-headless \
    faster-whisper \
    pymupdf \
    python-docx \
    beautifulsoup4

# 安装 Embedding 模型依赖（CPU-only PyTorch + transformers）
# 用于 L3 语义缓存的 Qwen/Qwen3-Embedding-0.6B 向量计算
RUN pip install --no-cache-dir \
    torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir \
    "transformers>=4.51.0"

# 预下载 Qwen3-Embedding-0.6B 模型权重（避免首次请求延迟）
RUN python -c "from transformers import AutoModel, AutoTokenizer; AutoTokenizer.from_pretrained('Qwen/Qwen3-Embedding-0.6B', trust_remote_code=True); AutoModel.from_pretrained('Qwen/Qwen3-Embedding-0.6B', trust_remote_code=True); print('Model cached')"

# 安装 Prompt Token 压缩（LLMLingua-2）
# 依赖前面已安装的 torch/transformers/nltk，只需装 llmlingua 本体
RUN pip install --no-cache-dir llmlingua>=0.2.0

# 安装 RAG 检索依赖（LlamaIndex + Qdrant 向量存储适配器 + 本地 HuggingFace embedding）
# RAGRetrieverPlugin 缺此依赖会降级为 passthrough
# huggingface embedding 让 RAG 复用本地 Qwen3-Embedding-0.6B，不再强依赖 OPENAI_API_KEY
RUN pip install --no-cache-dir \
    "llama-index>=0.10.0" \
    "llama-index-vector-stores-qdrant>=0.2.0" \
    "llama-index-embeddings-huggingface>=0.2.0" \
    qdrant-client

# 安装对话历史压缩依赖（LangChain classic + OpenAI 后端）
# langchain 1.x 已将 ConversationSummaryBufferMemory 等 Legacy Memory 类移到 langchain-classic
# ConvCompressorPlugin 缺此依赖会降级为 passthrough
RUN pip install --no-cache-dir \
    langchain-classic \
    langchain-openai

# 复制源码
COPY aigateway-core/src/aigateway_core /app/aigateway_core
COPY aigateway-api/src/aigateway_api /app/aigateway_api

EXPOSE 8000

CMD ["uvicorn", "aigateway_api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1"]
