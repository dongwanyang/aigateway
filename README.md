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
客户端 (OpenAI SDK / CLI / IDE)
        │
        ▼
┌──────────────────────────────────────────────────────────────────┐
│                      AI Gateway API (FastAPI :8000)               │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │              理解型管道 (Understanding Pipeline)             │  │
│  │                                                            │  │
│  │  PII Detector → Media Optimizer → Prompt Cache →           │  │
│  │  Semantic Cache → RAG Retriever → Conv Compressor →        │  │
│  │  Prompt Compress (LLMLingua-2) → Model Router → LLM       │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │           生成型管道 (Generation Optimization Pipeline)      │  │
│  │                                                            │  │
│  │  AI Director → Intent Evaluator → Token Compressor (CLIP)  │  │
│  │  → Draft Generator (ComfyUI) → Gen Model Router →         │  │
│  │  Cost Tracker                                              │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
├──────────────────────────────────────────────────────────────────┤
│  基础设施: Redis (缓存+队列) │ Qdrant (向量检索) │ Prometheus    │
└──────────────────────────────────────────────────────────────────┘
        │
        ▼
   OpenAI / Anthropic / DeepSeek / Agnes AI / Gemini / Ollama
```

---

## 双管线详解

### 理解型管道

处理 Chat/Completion 请求（对话、问答、代码生成等）：

| Stage | 插件 | 功能 | 开源集成 |
|-------|------|------|---------|
| 0 | PII Detector | 20+ 类敏感信息脱敏 | 自研正则 |
| 0 | Media Optimizer | 图片OCR/视频转录/音频识别/文档解析 | PaddleOCR + Unstructured |
| 1 | Prompt Cache | 精确匹配缓存 (L1 LRU + L2 Redis) | — |
| 1 | Semantic Cache | 向量语义缓存 (L3 Qdrant) | Qwen3-Embedding |
| 2 | RAG Retriever | 知识库检索增强 | LlamaIndex + Qdrant |
| 2 | Conv Compressor | 长对话历史摘要压缩 | LangChain Memory |
| 2.5 | Prompt Compress | Token 级精简压缩 | LLMLingua-2 |
| 3 | Model Router | 智能路由 + fallback | LiteLLM |

### 生成型管道

处理图片/视频生成请求，通过 6 大策略降低生成成本：

| 插件 | 功能 | 开源集成 |
|------|------|---------|
| AI Director | Prompt 结构化改写 | LLM API 调用 |
| Intent Evaluator | 复杂度评分 (0-100) | 自研规则引擎 |
| Token Compressor | 视觉特征提取 + 缓存 | CLIP (ViT-L-14) |
| Draft Generator | 低分辨率预览 → 确认后 upscale | ComfyUI API |
| Gen Model Router | 按模态/能力/价格动态路由 | 自研规则引擎 |
| Cost Tracker | 实时成本节省计算 | Prometheus |

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
git clone <repo-url> && cd gateway2

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

以下步骤在 **Ubuntu 26.04 + Python 3.14 系统环境** 上完整验证通过（`uv` 拉一个 3.12 独立解释器，不污染系统 python）。其他发行版可自行替换 Python 3.12 的获取方式（`pyenv install 3.12` / `conda create -n gw python=3.12` / 源码编译均可）。

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
cd gateway2
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
pip install -e "aigateway-core[llmlingua]"        # Prompt Token 压缩（LLMLingua-2）
pip install -e "aigateway-core[clip]"             # CLIP 视觉特征提取
pip install -e "aigateway-core[comfyui]"          # ComfyUI 图片/视频生成
pip install -e "aigateway-core[llamaindex]"       # LlamaIndex RAG 检索
pip install -e "aigateway-core[langchain]"        # LangChain 对话历史压缩
pip install -e "aigateway-core[paddleocr]"        # PaddleOCR 中文 OCR
pip install -e "aigateway-core[unstructured]"     # Unstructured 文档解析
pip install -e "aigateway-core[all-integrations]" # 一次装全部（推荐生产环境）

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
uvicorn aigateway_api.main:app --host 0.0.0.0 --port 8000 --reload \
  --app-dir aigateway-api/src

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
| 启动时 `ModuleNotFoundError: No module named 'lz4'` 或 `cachetools` | 旧版 `aigateway-core` 没声明这两个依赖。重新 `pip install -e .` 刷新到当前版本，或临时 `pip install lz4 cachetools` 兜底。 |
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
gateway2/
├── aigateway-core/                    # 共享核心库
│   ├── pyproject.toml                 # 依赖声明 + 7 个可选集成 extras
│   └── src/aigateway_core/
│       ├── pipeline.py                # 异步插件管线引擎 (拓扑排序)
│       ├── plugin_registry.py         # 插件注册、依赖校验
│       ├── context.py                 # PipelineContext 共享状态
│       ├── config.py                  # YAML 配置 + 环境变量 + 热重载
│       ├── integration_configs.py     # 7 个开源集成配置 dataclass
│       ├── caching.py                 # 四级缓存 (L1 LRU/L2 Redis/L3 Qdrant/L4 Media)
│       ├── security.py               # API Key + 配额 + PII 检测
│       ├── litellm_bridge.py          # LiteLLM 封装
│       ├── circuit_breaker.py         # per-provider 熔断器
│       ├── rate_limiter.py            # 滑动窗口限流
│       ├── tracing.py                 # OpenTelemetry
│       ├── metrics.py                 # Prometheus 指标
│       ├── redis_client.py            # Redis 异步连接池
│       ├── qdrant_client.py           # Qdrant 客户端
│       │
│       ├── plugins/                   # 理解型管道扩展插件
│       │   ├── rag_retriever_plugin.py    # LlamaIndex RAG 检索
│       │   └── conv_compressor_plugin.py  # LangChain 对话压缩
│       │
│       ├── media/                     # Media Optimization Layer (MOL)
│       │   ├── mol.py                 # 多模态处理入口
│       │   ├── plugin.py             # MOL 管线集成
│       │   ├── detector.py            # MIME/URL 类型检测
│       │   ├── cache.py               # L4 媒体缓存
│       │   └── pipelines/
│       │       ├── image.py           # 图片: 缩放/压缩/OCR(PaddleOCR)/Caption
│       │       ├── video.py           # 视频: 关键帧/转录(Whisper)
│       │       ├── audio.py           # 音频: 语音识别
│       │       └── document.py        # 文档: Unstructured 解析/分块
│       │
│       └── generation_optimization/   # 生成优化层
│           ├── config.py              # 优化策略配置
│           ├── models.py              # 数据结构
│           ├── metrics.py             # 成本追踪 + Prometheus
│           ├── strategies/            # 策略层
│           │   ├── ai_director.py     # Prompt 改写
│           │   ├── intent_evaluator.py# 复杂度评估
│           │   ├── model_router.py    # 生成模型路由
│           │   ├── token_compressor.py# CLIP 视觉压缩
│           │   ├── draft_generator.py # ComfyUI Draft-to-HiRes
│           │   └── feature_cache.py   # 特征向量 Redis 缓存
│           └── plugins/               # 插件层
│               ├── ai_director_plugin.py
│               ├── intent_evaluator_plugin.py
│               ├── token_compressor_plugin.py
│               ├── draft_generator_plugin.py
│               ├── gen_model_router_plugin.py
│               └── cost_tracker_plugin.py
│
├── aigateway-api/                     # FastAPI 服务
│   ├── Dockerfile                     # Python 3.12 + Tesseract + FFmpeg + PyTorch
│   └── src/aigateway_api/
│       ├── main.py                    # App 入口 + lifespan
│       ├── openai_compat.py           # /v1/chat/completions
│       ├── admin_routes.py            # API Key CRUD
│       ├── template_routes.py         # Prompt 模板 CRUD
│       ├── draft_routes.py            # Draft confirm/reject
│       ├── auth_middleware.py         # 认证中间件
│       └── streaming.py              # SSE 流式响应
│
├── aigateway-cli/                     # CLI 工具
│   └── src/aigateway_cli/
│       ├── __main__.py                # aigateway chat / run
│       ├── chat.py                    # 交互式对话
│       └── run.py                     # 单次请求
│
├── control-panel/                     # React 控制面板
│   ├── package.json                   # React 18 + Vite + TailwindCSS
│   └── src/pages/                     # Overview/Plugins/Costs/Cache/Logs
│
├── tests/                             # 582+ 测试
├── config.yaml                        # 唯一配置文件（含 API Key、插件、基础设施）
├── docker-compose.yml                 # 6 服务编排
└── .gitignore
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
  - name: model_router
    enabled: true
    depends_on: [prompt_compress]

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

### 管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET/POST/DELETE | `/admin/api-keys` | API Key CRUD |
| POST/GET/PUT/DELETE | `/templates` | Prompt 模板 CRUD |
| POST | `/drafts/{draft_id}/action` | Draft 确认/拒绝 |

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
| redis | 6379 | 缓存 + API Key + 特征向量 + Draft 暂存 |
| qdrant | 6333 | 向量数据库 (语义缓存 + RAG) |
| prometheus | 9090 | 指标采集 (30 天保留) |
| grafana | 3001 | 可视化面板 (admin/admin) |

---

## 开发

### 运行测试

```bash
python -m pytest tests/ -v          # 全部 582+ 测试
python -m pytest tests/ -x -q       # 快速模式（首个失败停止）
```

### 代码规范

- Python 3.12，全量类型注解，async/await 优先
- 插件接口：`async execute(ctx: PipelineContext) -> PipelineContext`
- Fail-open：所有插件故障时透传，不阻断请求
- 结构化日志：trace_id + request_id 贯穿全链路

### 新增插件

```python
class MyPlugin:
    name = "my_plugin"
    enabled = True
    depends_on = ["semantic_cache"]

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        # 你的逻辑
        return ctx
```

在 `config.yaml` 的 `plugins` 列表中添加即可，PipelineEngine 自动拓扑排序。

### 配置 Claude Code 的 LSP 代码智能（可选，推荐）

如果你用 Claude Code 开发本项目，配置 LSP 后 Claude 能精准查找定义/引用/符号/类型，比 grep 扫描更快更准、消耗上下文更少。本项目以 TypeScript（`control-panel/`）和 Python（`aigateway-api/`、`aigateway-core/`）为主，建议同时装 `typescript-lsp` 和 `pyright-lsp`。

#### 1. 安装 LSP server 二进制（全局）

```bash
# Python：pyright
npm install -g pyright          # 或 pip install pyright / pipx install pyright

# TypeScript / JavaScript：typescript-language-server + typescript
npm install -g typescript-language-server typescript

# （可选）Go：gopls
go install golang.org/x/tools/gopls@latest
```

验证：

```bash
pyright --version                       # 应输出 1.1.x
typescript-language-server --version    # 应输出 5.x
tsc --version                           # 应输出 6.x
```

> 注意：这些二进制必须在 Claude Code 启动时所在的 PATH 中。若用 nvm/包管理器装的，确认 `which pyright` 能找到。

#### 2. 在 Claude Code 里安装 LSP 插件

```bash
claude plugin install pyright-lsp@claude-plugins-official
claude plugin install typescript-lsp@claude-plugins-official
claude plugin install gopls-lsp@claude-plugins-official      # 可选
```

在 `~/.claude/settings.json` 中启用并打开 LSP 工具：

```json
{
  "env": {
    "ENABLE_LSP_TOOL": "1"
  },
  "enabledPlugins": {
    "pyright-lsp@claude-plugins-official": true,
    "typescript-lsp@claude-plugins-official": true,
    "gopls-lsp@claude-plugins-official": true
  }
}
```

#### 3. 验证插件被正确识别

> 这一步很关键：LSP 插件**不通过** `.claude-plugin/plugin.json` 的 hooks/agents 注册，用的是 LSP 专用机制。所以插件缓存目录里只有 `LICENSE` + `README`、没有 `plugin.json` 是**正常的**，不要误判为损坏。真正的检查方式是用 `claude plugin details`：

```bash
claude plugin details pyright-lsp@claude-plugins-official
claude plugin details typescript-lsp@claude-plugins-official
```

正确输出应包含一行：

```
LSP servers (1)  pyright  (out-of-process tooling; no model context cost)
LSP servers (1)  typescript  (out-of-process tooling; no model context cost)
```

如果 `details` 报 `Plugin not found`，说明缓存损坏，重装即可：

```bash
claude plugin uninstall typescript-lsp@claude-plugins-official
claude plugin install  typescript-lsp@claude-plugins-official
```

marketplace 看起来陈旧时刷新源：

```bash
claude plugin marketplace update claude-plugins-official
```

#### 4. 重启会话生效

`env` 变量和 LSP 注册表**只在 Claude Code 进程启动时加载一次**。装完插件、改完 settings.json 后，必须**完全退出当前会话、新开一个会话**——运行中的会话不会热加载，LSP 工具会一直返回 `No LSP server available for file type: .ts`。

新会话里第一次调用 LSP 可能会慢几秒（server 首次握手 + 索引），属正常现象。

#### 5. 可用能力

| 操作 | 适用场景 |
|------|---------|
| `goToDefinition` | 跳转符号定义 |
| `findReferences` | 查找所有调用方 |
| `hover` | 查看类型/文档 |
| `documentSymbol` | 列出文件内所有符号（替代通读整文件） |
| `workspaceSymbol` | 跨工作区按名搜符号 |
| `goToImplementation` | 查找接口实现 |
| `prepareCallHierarchy` + `incomingCalls` / `outgoingCalls` | 调用层级分析 |

文件类型覆盖：`.ts/.tsx/.js/.jsx/.mts/.cts/.mjs/.cjs`（typescript-lsp）、`.py/.pyi`（pyright-lsp）、`.go`（gopls-lsp）。LSP 不适用时（搜字符串/正则、查注释/配置/文档）退回 grep/glob。

---

## 许可证

MIT
