# AI Gateway

> Enterprise Multimodal AI Gateway — 位于客户端和 LLM 提供商之间的智能代理，支持文本理解和多模态生成双管线优化。

[![Python](https://img.shields.io/badge/Python-3.12%2B-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

---

## 一句话介绍

把现有 AI 应用的 `OPENAI_BASE_URL` 指向 AI Gateway，即可自动享受 **多模态优化、提示词压缩（LLMLingua-2）、RAG 检索增强、对话历史摘要、三级缓存、智能路由、PII 脱敏、生成成本优化** 等能力，无需修改一行业务代码。

---

## 架构总览

```
         Client (OpenAI SDK / CLI / IDE)
                    │
                    ▼
        ┌─────────────────────────────────┐
        │        AI Gateway (:8000)       │
        │       auth · trace · quota      │
        └────────────────┬────────────────┘
                         ▼
        ┌─────────────────────────────────┐
        │        RequestDispatcher        │
        │          (orchestrator)         │
        └────────────────┬────────────────┘
                         ▼
        ┌─────────────────────────────────┐
        │   Shared Prefix (all requests)  │
        │      Media -> PII -> Cache      │
        └────────────────┬────────────────┘
                         ▼
                 classify_request
                         │
               ┌─────────┴─────────┐
               ▼                   ▼
        ┌─────────────┐     ┌─────────────┐
        │Understanding│     │  Generation │
        │  RAG + Conv │     │Director -> …│
        │  Compressor │     │   -> Cost   │
        └──────┬──────┘     └──────┬──────┘
               └─────────┴─────────┘
                         ▼
        ┌─────────────────────────────────┐
        │          LiteLLMBridge          │
        │     (auto model resolution)     │
        └────────────────┬────────────────┘
                         ▼
               OpenAI · Anthropic · DeepSeek
                 · Agnes · Gemini · Ollama

          缓存: L1(LRU) -> L2(Redis) -> L3(Qdrant)
```

**总分总编排**：共享前置（Media / PII / Cache / Compress，所有请求必经）-> `classify_request` (async LLM intent prediction) 分流 → understanding | generation:image | generation:video -> 理解型管线（RAG + Conv Compressor）或生成型管线（Director -> Intent -> Token -> Draft -> Router -> Cost 六插件链）-> 配额校验 -> LiteLLMBridge 统一出口（含 capabilities 池过滤 + image/video 生成路径）。`model_router` 插件已移除，路由在 LiteLLMBridge 内完成。

---

## 快速开始

### 前置要求

- **Python 3.12**（必须；paddlepaddle / llama-index-vector-stores-qdrant 目前无 3.13/3.14 wheel）
- Node.js 20+（本地跑前端时需要）
- Docker（用于快速起 Redis / Qdrant，或方式一的整套编排）
- Redis 7+
- Qdrant 1.7+（语义缓存 + RAG 使用；不启也可以跑，语义缓存会 fail-open）

> ⚠️ **不要用 Python 3.13/3.14**：paddleocr 依赖的 `paddlepaddle` 目前最新版（3.3.1）在 PyPI 上没有 cp313/cp314 wheel，`pip install` 会直接报 `No matching distribution found`。项目 Docker 镜像用的是 `python:3.12-slim`，本地开发请对齐。

### 方式一：Docker Compose（推荐）

```bash
# 1. 克隆项目
git clone <repo-url>

# 2. 创建 .env 并填入你的 LLM 提供商 API Key
cp .env.example .env
nano .env   # 至少填一个:AGNES_API_KEY 或 DEEPSEEK_API_KEY

# 3. 一键启动 6 个服务（首次构建约 10-15 分钟,后续改代码重建秒级）
docker compose up -d --build
#   或用快速启动脚本(自动引导建 .env + 健康检查):
#   bash scripts/quickstart.sh --build

# 4. 访问
# API Gateway:   http://localhost:8000
# 控制面板:      http://localhost:3000
# Prometheus:    http://localhost:9090
# Grafana:       http://localhost:3001 (admin/admin)
```

> 💡 **不填 API Key 也能启动**：`config.yaml` 中所有密钥用 `${VAR:-}` 引用，未设时优雅降级为空。Gateway 能正常启动（插件 fail-open），但调用 LLM 会鉴权失败 —— 填好 `.env` 后 `docker compose restart gateway` 即可。
>
> 📋 完整安装/配置/排查指引见 [INSTALL.md](INSTALL.md)。

### 方式二：本地开发

以下步骤在 **Ubuntu 26.04** 上完整验证通过（`uv` 拉一个 Python 3.12 独立解释器，系统自带的 Python 3.12/3.14 不会被项目使用）。其他发行版可自行替换 Python 3.12 的获取方式（`pyenv install 3.12` / `conda create -n gw python=3.12` / 源码编译均可）。

```bash
# ------------------------------------------------------------------
# 0. 准备 Python 3.12（如果系统已经是 3.12，可以跳过这一段）
# ------------------------------------------------------------------
# 用 uv 拉一个独立的 3.12 解释器（不需要 sudo，也不会替换系统 python）
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"        # 建议同时写进 ~/.bashrc
uv python install 3.12

# ------------------------------------------------------------------
# 1. 创建并激活虚拟环境（--seed 会顺带装好 pip）
# ------------------------------------------------------------------
uv venv --python 3.12 --seed .venv
source .venv/bin/activate
python --version    # 应输出 Python 3.12.x

# ------------------------------------------------------------------
# 2. 安装核心库（顺序重要：core 先装）
# ------------------------------------------------------------------
cd aigateway-core && pip install -e . && cd ..
cd aigateway-api  && pip install -e . && cd ..
cd aigateway-cli  && pip install -e . && cd ..

# ------------------------------------------------------------------
# 3. 安装可选集成（按需选择；all-integrations 会拖 ~5GB 依赖，含 torch/CUDA/paddle）
# ------------------------------------------------------------------
# 3. 按需安装可选集成（见下方「开源集成清单」表；一次装全：pip install -e "aigateway-core[all-integrations]"）

# ------------------------------------------------------------------
# 4. 编辑 config.yaml，填入 API Key（providers 节）
#    建议改成 ${AGNES_API_KEY} / ${DEEPSEEK_API_KEY} 引用环境变量
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# 5. 启动基础设施（Redis 必须；Qdrant 是语义缓存/RAG 才需要）
# ------------------------------------------------------------------
docker run -d --name redis  -p 6379:6379           redis:7-alpine
docker run -d --name qdrant -p 6333:6333 -p 6334:6334 qdrant/qdrant:latest

# ------------------------------------------------------------------
# 6. 启动 API 服务（从项目根目录启动，确保 config.yaml 可被找到）
# ------------------------------------------------------------------
uvicorn aigateway_api.main:create_app --factory --host 0.0.0.0 --port 8000 --reload

# ------------------------------------------------------------------
# 7. 启动前端（另一个终端）
# ------------------------------------------------------------------
cd control-panel && npm install && npm run dev
# Vite dev server: http://localhost:5173
# 已配置 /aigateway/* 代理到 http://localhost:8000
```

#### 常见问题排查

| 现象 | 原因 & 解决 |
|------|-------------|
| `pip install` 报 `error: externally-managed-environment` | 没进虚拟环境。执行 `source .venv/bin/activate` 后再装。 |
| `paddlepaddle` 报 `No matching distribution found (from versions: none)` | Python 版本不是 3.12。用 `python --version` 核对，参考上面第 0 步换 3.12。 |
| 启动时 `ModuleNotFoundError: No module named 'lz4'` 或 `cachetools` | 确保已激活虚拟环境后重新 `pip install -e .` 安装核心库。 |
| 启动日志 `Qdrant 连接失败，语义缓存功能不可用` | 未启动 Qdrant。执行上面第 5 步的 `docker run qdrant`。不装也可以运行，只是没有 L3 语义缓存。 |
| 启动日志 `providers.xxx.api_key 疑似明文密钥` | `config.yaml` 里写了明文 key。建议改成 `${ENV_VAR}` 形式，并在启动前 `export AGNES_API_KEY=...`。 |
| `[Errno 98] address already in use` | 8000 端口被占，`lsof -i:8000` 找到旧进程 kill 掉。 |

### 验证

```bash
# 列出模型
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o"

# 发送聊天请求
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o" \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"你好"}]}'

# CLI 交互
aigateway chat
aigateway run --prompt "你好，世界"
```

---

## 项目结构

```
aigateway/
├── aigateway-core/src/aigateway_core/   # 共享核心库
│   ├── prefix/          # 共享前置层（所有请求必经）：pii / cache / media
│   ├── dispatch/        # RequestDispatcher + PipelineEngine + classify_request
│   ├── pipelines/
│   │   ├── understanding/   # rag / conversation / compression / code_rag
│   │   └── generation/      # 6 插件链：director / intent / token / draft / routing_signals / cost
│   ├── route/           # LiteLLMBridge / SSE / metrics / model_resolution
│   └── shared/          # config / tracing / redis / qdrant / auth(sqlite_store)
├── aigateway-api/src/aigateway_api/     # FastAPI 服务（openai_compat / admin_routes / *_routes / middlewares）
├── aigateway-cli/src/aigateway_cli/     # CLI（chat / run / session / codegraph）
├── control-panel/src/                   # React 控制面板（10 个页面）
├── tests/                               # 82+ 测试文件
├── config.yaml                          # 唯一配置文件
└── docker-compose.yml                   # 6 服务编排
```

---

## 配置说明

### config.yaml 核心节

项目使用单一 `config.yaml` 文件管理所有运行时配置，支持环境变量覆盖和文件监听热重载。

```yaml
# 插件管线（理解型管道执行顺序由 depends_on 拓扑排序决定）
plugins:
  - name: pii_detector
    enabled: true
  - name: prompt_cache
    enabled: true
  - name: semantic_cache
    enabled: true
    depends_on: [prompt_cache]
    config:
      embedding_model: Qwen/Qwen3-Embedding-0.6B
      threshold: 0.95
  - name: rag_retriever           # LlamaIndex RAG（需安装 llamaindex extra）
    enabled: false
    depends_on: [semantic_cache]
    config:
      top_k: 5
      similarity_threshold: 0.7
  - name: conv_compressor         # LangChain 对话压缩（需安装 langchain extra）
    enabled: false
    depends_on: [semantic_cache]
    config:
      max_history: 20
      summary_model: gpt-4o-mini
  - name: prompt_compress         # LLMLingua-2（需安装 llmlingua extra）
    enabled: true
    depends_on: [rag_retriever, conv_compressor]
    config:
      compression_ratio: 0.5
      model_name: "microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank"

# 多模态处理
media_optimization:
  enabled: true
  image:
    ocr_backend: paddleocr       # "paddleocr" | "tesseract"
    paddleocr:
      lang: ch
  document:
    unstructured:                 # Unstructured 统一解析
      strategy: auto
      languages: [chi_sim, eng]

# 生成优化
generation_optimization:
  token_compressor:
    clip:
      model_name: "openai/clip-vit-large-patch14"
      device: cpu
  draft_workflow:
    comfyui:
      server_url: "http://localhost:8188"
      execution_timeout: 300
```

### 环境变量

所有配置通过 `config.yaml` 管理。环境变量仅在需要覆盖 YAML 值时使用（`AI_GATEWAY_` 前缀，优先级高于 YAML）：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `AI_GATEWAY_REDIS_URL` | Redis 地址 | `redis://localhost:6379/0` |
| `AI_GATEWAY_QDRANT_URL` | Qdrant 地址 | `http://localhost:6333` |
| `AI_GATEWAY_PORT` | 监听端口 | `8000` |
| `AI_GATEWAY_LOG_LEVEL` | 日志级别 | `info` |
| `AI_GATEWAY_PROMPT_COMPRESS_COMPRESSION_RATIO` | 压缩率 | `0.5` |
| `AI_GATEWAY_CLIP_DEVICE` | CLIP 设备 | `cpu` |
| `OPENAI_API_KEY` | OpenAI 密钥 | — |
| `AGNES_API_KEY` | Agnes AI 密钥 | — |

---

## 开源集成清单

所有集成均为**可选依赖**，未安装时自动降级为 passthrough 模式（fail-open）：

| 集成 | 包名 | 安装命令 | 用途 |
|------|------|---------|------|
| LLMLingua-2 | `llmlingua` | `pip install -e ".[llmlingua]"` | Prompt Token 压缩 |
| CLIP | `transformers` + `torch` | `pip install -e ".[clip]"` | 视觉特征提取 |
| ComfyUI | `websockets` + `httpx` | `pip install -e ".[comfyui]"` | 图片/视频生成 |
| LlamaIndex | `llama-index` | `pip install -e ".[llamaindex]"` | RAG 向量检索 |
| LangChain | `langchain` | `pip install -e ".[langchain]"` | 对话历史摘要 |
| PaddleOCR | `paddleocr` | `pip install -e ".[paddleocr]"` | 中文 OCR |
| Unstructured | `unstructured` | `pip install -e ".[unstructured]"` | 文档解析 |

全部安装：`pip install -e ".[all-integrations]"`

---

## API 接口

### OpenAI 兼容

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/chat/completions` | 聊天补全（流式/非流式，多模态） |
| GET | `/v1/models` | 列出可用模型 |
| POST | `/v1/embeddings` | 嵌入向量 |
| GET | `/v1/videos/{video_id}` | 视频生成任务状态查询 |

### 管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET/POST/PUT/DELETE | `/admin/api-keys` | API Key CRUD |
| POST/GET/PUT/DELETE | `/templates` | Prompt 模板 CRUD |
| POST | `/admin/drafts/{draft_id}/action` | Draft 确认/拒绝 |
| GET | `/admin/chat/tasks` | 异步任务列表 |
| GET | `/admin/logs` | 请求日志 |
| GET/PUT/DELETE | `/admin/cache/l3/*` | L3 语义缓存管理 |
| POST/GET | `/admin/rag/code/*` | Code RAG 导入与查询 |
| GET | `/admin/config/debug` | Debug 开关配置 |

### 基础设施

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/metrics` | Prometheus 指标 |
| GET | `/health` | 健康检查 |

---

## Docker Compose 服务

| 服务 | 端口 | 说明 |
|------|------|------|
| gateway | 8000 | FastAPI API (Python 3.12 + Tesseract + FFmpeg) |
| control-panel | 3000 | React 控制面板 (Nginx) |
| redis | 6379 | 缓存 + Draft 暂存 |
| qdrant | 6333 | 向量数据库 (语义缓存 + RAG) |
| prometheus | 9090 | 指标采集 (30 天保留) |
| grafana | 3001 | 可视化面板 (admin/admin) |

---

## 开发

### 运行测试

```bash
python -m pytest tests/ -v          # 全部 82+ 测试文件
python -m pytest tests/ -x -q       # 快速模式（首个失败停止）
```

### 代码规范

- Python 3.12，全量类型注解，async/await 优先
- 插件接口：`async execute(ctx: PipelineContext) -> PipelineContext`
- Fail-open：所有插件故障时透传，不阻断请求
- 结构化日志：trace_id + request_id 贯穿全链路

## 许可证

MIT
