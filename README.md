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

- Docker Engine/Desktop 与 Docker Compose v2
- Linux 或 Windows WSL2 的 Knowledge/Studio/Full：NVIDIA 驱动及
  NVIDIA Container Toolkit
- Apple Silicon：Docker Desktop、Python 3 与 Xcode Command Line Tools

### 方式一：Docker Compose（推荐）

```bash
# 1. 克隆项目
git clone <repo-url>

# 2. 默认从 GHCR 安装 Lite
bash scripts/quickstart.sh --edition lite

# Full：从 GHCR 安装，启用监控并安装批准模型
bash scripts/quickstart.sh --edition full --monitoring --install-models

# 从当前 checkout 构建完全相同的 targets
bash scripts/quickstart.sh --edition full --distribution source --build

# 3. 在自动创建的 .env 中填入至少一个提供商 API Key
nano .env

# 4. 访问（监控地址仅在向导中启用监控后存在）
# API Gateway:   http://localhost:8000
# 控制面板:      http://localhost:3000
```

> 💡 **不填 API Key 也能启动**：`config.yaml` 中所有密钥用 `${VAR:-}` 引用，未设时优雅降级为空。Gateway 能正常启动（插件 fail-open），但调用 LLM 会鉴权失败 —— 填好 `.env` 后 `docker compose restart gateway` 即可。
>
四档套餐是 Lite（Gateway/控制台/Redis）、Knowledge（加 Qdrant 与 GPU
Embedding）、Studio（加独立 ComfyUI）和 Full。Linux/Windows 使用独立
NVIDIA ComfyUI 容器；Apple Silicon 的核心栈仍在 Docker 中，ComfyUI 与
Embedding 以用户级 MPS 服务运行，Gateway 通过 `host.docker.internal`
访问。

源码模式会从对应版本的 GHCR 镜像导入 BuildKit inline cache。Python
依赖、PyTorch/CUDA 和系统包位于源码层之前，因此日常代码修改只会重新
复制并安装本地 Gateway 包；Gateway 与 ComfyUI 也会复用同一 CUDA/PyTorch
层。为保持这种增量构建速度，不要在每次开发构建后运行
`docker builder prune`，只在磁盘水位需要回收时清理构建缓存。
>
> 📋 完整安装/配置/排查指引见 [INSTALL.md](INSTALL.md)。

### npm 安装

不需要先手动克隆仓库。npm 安装器默认拉取 GHCR 镜像：

```bash
npm install -g aigateway-installer
aigateway-install --edition lite
aigateway-install --edition full --distribution source --build

# 或者一次性运行
npx aigateway-installer
```

npm 包不在 `postinstall` 阶段执行系统命令。旧 `--profile`、`--accelerator`、
`--add/--remove`、`--source/--docker` 接口已移除。

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
cd aigateway-api  && pip install -e ".[dev]" && cd ..
cd aigateway-cli  && pip install -e . && cd ..

# ------------------------------------------------------------------
# 3. 安装可选能力（从仓库根目录执行）
# ------------------------------------------------------------------
pip install -e "aigateway-api[rag]"          # RAG / Code RAG / 本地 Embedding
pip install -e "aigateway-api[vision,gpu]"   # OCR / 视频 / 超分辨率

# ------------------------------------------------------------------
# 4. 编辑 config.yaml，填入 API Key（providers 节）
#    建议改成 ${AGNES_API_KEY} / ${DEEPSEEK_API_KEY} 引用环境变量
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# 5. 启动基础设施（Redis 必须；Qdrant 是语义缓存/RAG 才需要）
# ------------------------------------------------------------------
docker run -d --name redis -p 6379:6379 redis/redis-stack:7.2.0-v18
docker run -d --name qdrant -p 6333:6333 -p 6334:6334 qdrant/qdrant:v1.13.4

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

先通过控制台创建 QA 专用 API Key，或从测试环境变量 / CI Secret 注入，不要把真实客户端 key 写入仓库文件。完整测试选择见 [docs/TESTING.md](docs/TESTING.md)，认证与 API Key QA 流程见 [docs/QA_AUTH_TESTING.md](docs/QA_AUTH_TESTING.md)。

```bash
export QA_API_KEY="<created-by-admin-api-keys>"

# 列出模型
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer ${QA_API_KEY:?missing QA_API_KEY}"

# 发送聊天请求
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${QA_API_KEY:?missing QA_API_KEY}" \
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
└── docker-compose.yml                   # 核心服务与可选 profiles
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
      server_url: "http://comfyui:8188"
      execution_timeout: 300
      required: true
      workflow_version: "image-v1"
      checkpoint_name: "sd_xl_base_1.0.safetensors"
      allowed_checkpoints: ["sd_xl_base_1.0.safetensors"]
      max_concurrency: 1
      min_free_gb: 30
      model_budget_gb: 30
      output_budget_gb: 10
      workflow_path: "/comfyui/workflows"
```

### ComfyUI 渐进式图片与视频生成

ComfyUI 是图片草稿与最终精修的必需后端。服务不可用、checkpoint
不在白名单或磁盘低于水位时，生成请求会明确失败，不会静默改走外部图片
或视频 API。

```bash
# 1. 宿主机与容器必须都能看到 GPU
nvidia-smi
docker run --rm --gpus all nvidia/cuda:13.0.1-base-ubuntu24.04 nvidia-smi

# 2. 许可证确认、80GB 下载水位与 SHA256 校验由安装器处理
bash scripts/quickstart.sh --edition studio --install-models

# 3. ComfyUI 管理界面仅绑定宿主机回环地址，不直接暴露公网
docker compose --profile comfy-container ps
```

本机可打开 `http://127.0.0.1:8188`。如果 Docker 运行在远程服务器，
先从你的电脑建立 SSH 隧道，再打开同一地址：

```bash
ssh -L 8188:127.0.0.1:8188 ubuntu@<服务器地址>
```

ComfyUI 本身不经过 Gateway 登录认证，因此不建议把
`COMFYUI_HOST_BIND` 改成 `0.0.0.0`。

草稿使用低分辨率、低采样步数；确认后上传已认可草稿，并复用相同
checkpoint、seed 与 prompt 执行 img2img 高清精修。模型默认预算 30GB、
输出预算 10GB、系统保留空间 30GB，输出超过保留期后自动清理。

视频请求先用同一 SDXL 草稿流程生成一张低成本关键帧；确认后将该关键帧、
prompt 和 seed 交给 `wan2.2-ti2v-5b-v1`，由 ComfyUI 原生
Wan2.2 TI2V 5B 工作流生成 MP4。默认使用适合 15GB 显存的
512×288、17 帧、8 fps 配置；失败时 fail-closed，不会重新调用 Agnes
`/videos`。可单独安装视频模型：

```bash
bash scripts/model-manager.sh install wan2.2-ti2v-5b
bash scripts/model-manager.sh install realesrgan-x4plus
bash scripts/model-manager.sh install qwen-image
```

### ComfyUI Manager 与 4K 保真

Studio/Full 的 ComfyUI 镜像固定版本预装官方 ComfyUI-Manager，并以
`normal` 安全级别启动。`comfyui/custom_nodes` 与 `comfyui/user` 会持久化，
因此第三方节点、Manager 配置、快照和安装记录在容器重建后仍会保留；
首次创建的空用户目录才会写入默认配置，后续启动不会覆盖管理员设置。
ComfyUI 端口默认只绑定本机，节点和高级工作流继续在原生 ComfyUI 页面管理。

聊天生成可选择“自动 / 本地 / 云端”与“标准 / 创意精修 / 4K 保真”。
4K 保真使用 ComfyUI Core 节点和批准的 `RealESRGAN_x4plus.pth`，保持宽高比、
不裁剪，最长边默认不超过 4096。模型不会在普通启动时静默下载；可显式运行：

```bash
bash scripts/model-manager.sh install realesrgan-x4plus
bash scripts/model-manager.sh verify realesrgan-x4plus
```

未安装 ComfyUI 的 Lite/Knowledge 版本仍可选择云端图片或视频模型。
Qwen-Image FP8 由三个文件组成，合计约 30.1GB；安装后中文图片提示词会优先
使用 Qwen-Image，未安装时继续由 AI Director 为 SDXL 做保真翻译和精简。

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

| 能力集 | 安装命令 | 用途 |
|--------|---------|------|
| `dev` | `pip install -e "aigateway-api[dev]"` | pytest、ruff、mypy |
| `rag` | `pip install -e "aigateway-api[rag]"` | LLMLingua、LlamaIndex、LangChain、Code RAG、本地 Embedding |
| `vision` | `pip install -e "aigateway-api[vision]"` | OCR、音视频解析、RealESRGAN |
| `gpu` | `pip install -e "aigateway-api[gpu]"` | PyTorch GPU/本地推理基础 |
| `all` | `pip install -e "aigateway-api[all]"` | 全部运行时能力（镜像与磁盘占用很大） |

Python 包及版本只在 `aigateway-core/pyproject.toml` 和
`aigateway-api/pyproject.toml` 中声明；Dockerfile 仅选择上述能力集。

---

## 基准测试

项目附带一套自验证基准测试套件，用于测量 token 节省、响应质量和可观测性。详见 [benchmarks/README.md](benchmarks/README.md)。

```bash
./benchmarks/run_benchmark.sh          # 运行全部文本场景
./benchmarks/run_benchmark.sh --judge   # 加入 LLM-as-judge 质量评分
```

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
| POST | `/admin/draft/{draft_id}/confirm` | Draft 确认 |
| POST | `/admin/draft/{draft_id}/reject` | Draft 拒绝 |
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
| grafana | 3001 | 可视化面板（密码由 Secret 注入） |

---

## 开发

### 运行测试

完整测试选择、认证 QA 和交付格式见：

- [docs/TESTING.md](docs/TESTING.md)
- [docs/QA_AUTH_TESTING.md](docs/QA_AUTH_TESTING.md)

常用命令：

```bash
python -m pytest tests/ -v          # 全部测试
python -m pytest tests/ -x -q       # 快速模式（首个失败停止）
```

### 代码规范

- Python 3.12，全量类型注解，async/await 优先
- 插件接口：`async execute(ctx: PipelineContext) -> PipelineContext`
- Fail-open：所有插件故障时透传，不阻断请求
- 结构化日志：trace_id + request_id 贯穿全链路

## 许可证

MIT
