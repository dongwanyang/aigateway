# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI Gateway is an OpenAI-compatible proxy gateway that sits between clients and LLM providers, adding token optimization, multi-tier caching, model routing, PII sanitization, circuit breaking, and cost tracking. Zero code changes required -- clients only need to change their `OPENAI_BASE_URL`.

Three usage modes:
1. **API Gateway** (FastAPI + Uvicorn :8000) -- the core service
2. **CLI tool** (`aigateway chat` / `aigateway run`) -- interactive REPL and single-shot requests
3. **Control Panel** (React SPA :3000) -- admin dashboard for monitoring and configuration

## Architecture at a Glance

```
Client (OpenAI SDK / CLI / IDE)
        │
        ▼
┌─────────────────────┐
│  aigateway-api       │  FastAPI + Uvicorn (:8000)
│  ├── main.py         │  App factory, lifespan init, router mounting, 两条管道 Engine 实例
│  ├── dispatcher.py   │  RequestDispatcher — 总分总「总入口」:分流 understanding|generation + 编排缓存/配额/LLM/回填
│  ├── openai_compat.py│  /v1/chat/completions 入口(调 dispatcher) + 辅助函数(_apply_*/_record_request_log 等)
│  ├── admin_routes.py │  API Key CRUD, quotas, plugin config, logs, RAG, L3 cache mgmt
│  ├── auth_middleware.py│ Bearer/x-api-key validation via KeyStore
│  ├── streaming.py    │  SSE generator + cached stream simulation
│  ├── routes.py       │  GET /metrics, GET /health
│  ├── draft_routes.py │  Draft-to-HiRes generation endpoints
│  ├── template_routes.py│ Prompt template management endpoints
│  └── rate_limiter.py │  IP-based rate limiting middleware
├── aigateway-core     │  Shared library (imported by API + CLI)
│  ├── pipeline.py     │  PipelineEngine(按 pipeline_kind 装载) + 经典插件(PII/cache/semantic/compress/rag/conv/media)
│  ├── plugin_registry.py│  Registration, topological sort, lifecycle
│  ├── context.py      │  PipelineContext — shared request state with typed namespaces
│  ├── caching.py      │  CacheManager: L1(LRU) → L2(Redis+LZ4) → L3(Qdrant), plus LightweightReranker / CrossEncoderRerankers
│  ├── security.py     │  KeyStore (Redis-backed quotas/rate-limits), PIIDetector (20+ PII patterns)
│  ├── litellm_bridge.py│  Wraps LiteLLM Router for multi-provider calls with fallback chains
│  ├── circuit_breaker.py│  Per-provider CLOSED/OPEN/HALF-OPEN state machine
│  ├── config.py       │  YAML loader, env var overrides, Watchdog hot-reload
│  ├── metrics.py      │  Prometheus counters/histograms/gauges
│  ├── tracing.py      │  OpenTelemetry trace integration
│  ├── logger.py       │  Structured JSON logging via structlog
│  ├── redis_client.py │  Async Redis connection pool
│  ├── qdrant_client.py│  Async Qdrant HTTP client
│  ├── exceptions.py   │  GatewayError → AuthError / QuotaExceededError / CircuitBreakerOpenError
│  ├── generation_optimization/ │  New generation optimization layer (6 plugins, 8 strategies)
│  │   ├── config.py   │  GenerationOptimizationConfig with dataclass validation + hot-reload watcher
│  │   ├── plugins/    │  ai_director, intent_evaluator, token_compressor, draft_generator, gen_model_router, cost_tracker
│  │   ├── strategies/ │  AIDirector, IntentEvaluator, TokenCompressor, DraftGenerator, ModelRouter, PromptConfirmation, PromptTemplateManager, FeatureCache
│  │   ├── models.py   │  CompressionResult, GenerationMetadata, etc.
│  │   ├── metrics.py  │  GenerationCostTracker + Prometheus integration
│  │   ├── api_key_groups.py │  API Key group aggregation for cost metrics
│  │   └── exceptions.py
│  └── media/          │  Media Optimization Layer (V2)
│      ├── plugin.py   │  MediaOptimizationPlugin — integrates into PipelineEngine
│      ├── mol.py      │  MediaOptimizationLayer — orchestrates pipelines
│      ├── cache.py    │  MediaCacheManager (Redis-backed)
│      ├── config.py   │  Image/Video/Audio/Document pipeline configs
│      ├── detector.py │  MIME/type detection
│      ├── generation.py│  Agnes image/video generation helpers
│      ├── types.py    │  MediaContent, MediaType enum
│      └── pipelines/  │  ImagePipeline (resize/OCR/caption), VideoPipeline, AudioPipeline, DocumentPipeline
├── aigateway-cli      │  CLI tool (aigateway chat / aigateway run)
│  ├── __main__.py     │  argparse entry point
│  ├── chat.py         │  Interactive REPL session
│  ├── run.py          │  Single-shot request
│  └── session.py      │  Named session persistence
└── control-panel      │  React SPA (Vite + TypeScript + TailwindCSS + Recharts)
    ├── src/App.tsx     │  React Router: /, /plugins, /costs, /quotas, /cache, /logs, /knowledge, /config, /models
    ├── src/api/client.ts│ VITE_API_BASE-prefixed fetch calls, Prometheus text parser
    ├── src/pages/      │  9 page components (Overview, Models, Plugins, Costs, Quotas, Cache, Logs, Knowledge, Config)
    ├── src/components/ │  Layout, Card, ErrorBoundary, PageErrorBoundary
    └── src/hooks/      │  useAuth, usePoll, useTheme
```

### aigateway 智能体的两个入口(目标形态)

> ⭐ **未来最终形态** — 当前入口 A ✅,入口 B 🚧(spec: `docs/superpowers/specs/2026-07-05-control-panel-chat-agent-design.md`)。入口 A 完成,入口 B 由本 spec 推进。未来若新增第三入口(MCP / IDE 插件 / bot),右侧继续扩,左侧内核不变。

aigateway 不是"OpenAI 代理 + 附加聊天窗",而是**一个智能体本体,面向机器和面向人各开一个入口**。两个入口共享 dispatcher + 两条管道 + LiteLLM 出口,入口 B 额外挂 agent tool loop(HITL / audit / trust)。

```
                        ┌─────────────────────────────────────┐
                        │      aigateway 智能体                 │
                        └─────────────────────────────────────┘

╔═══════════════════════════════╗          ╔═══════════════════════════════╗
║  入口 A(机器面,现有)          ║          ║  入口 B(真人面,🚧 本 spec)    ║
║  /v1/chat/completions         ║          ║  /admin/agent/chat            ║
║  OpenAI-兼容 · JSON/SSE       ║          ║  SSE · 控制台 /chat 页面       ║
╚═══════════════════════════════╝          ╚═══════════════════════════════╝
             │                                            │
             ▼                                            ▼
             │                                   ┌────────────────────┐
             │                                   │ ChatRouter         │
             │                                   │ (task/gen/und 分类)  │
             │                                   └────────────────────┘
             │                                            │
             │                       ┌────── task ────────┤
             │                       │                    │
             │                       ▼                    │
             │                ┌────────────────┐          │
             │                │ AgentLoop      │          │
             │                │ (tool calling) │          │
             │                │ + HITL/Audit   │          │
             │                └────────┬───────┘          │
             │                         │ loopback         │ 直连
             ▼                         ▼                  ▼
   ┌───────────────────────────────────────────────────────────┐
                     RequestDispatcher.dispatch()
          ① media_optimization → PII    ② classify_request
          ③ 理解管道  or  生成管道  →  LiteLLMBridge
   └───────────────────────────────────────────────────────────┘
        │            │           │             │
        ▼            ▼           ▼             ▼
     OpenAI     Anthropic     Agnes       DeepSeek/…
   (gpt-4o…)   (claude…)   (image/video)       

```

对比:

| 维度 | 入口 A | 入口 B |
|---|---|---|
| 面向谁 | 机器(SDK/CLI/IDE/curl) | 控制台真人(admin + 终端用户) |
| protocol | OpenAI chat completions | SSE 事件流 |
| 能力 | 理解 + 生成管道 | tool 循环 + 理解 + 生成管道 |
| `tools` 参数 | 透传上游,不代执行 | AgentLoop 代执行 |
| HITL | ✗ | ✓(写工具) |
| 改运维状态 | ✗ | ✓(admin 工具集) |

详见 spec 1.1 节完整对照表。

## Plugin Pipeline Flow

Request enters FastAPI → auth_middleware validates API key → two parallel processing paths:

### Path 1: Built-in Pipeline (pipeline.py)
1. **pii_detector** — scan for 20+ PII patterns (email, phone, credit card, Chinese ID, passwords, API keys, connection strings), sanitize/reject/hash
2. **prompt_cache** — exact-match cache (L1 → L2 → L3)
3. **semantic_cache** — vector similarity on missed prompts (Qdrant, cosine ≥ 0.95)
4. **prompt_compress** — LLMLingua-2 based prompt compression (real implementation; `compression_ratio`, `device`, `target_token` configurable)
5. **rag_retriever** — Qdrant knowledge base retrieval (default enabled with local fallback)
6. **conv_compressor** — conversation history summarization for long sessions (default enabled with local defaults)

> 注:经典 `model_router` 插件已彻底删除(见下方 Architecture Decisions)。真路由由 bridge 的 auto 解析承担;`classify_request` 只做模态分流。`gen_model_router`(生成管道)是另一个不同插件,勿混淆。

Short-circuit via `ctx.should_stop = True` at any stage. Non-critical plugin failures are fail-open.

### Path 2: Generation Optimization Layer (newer, pluggable)
Dependency chain (priority-ordered):
1. **ai_director** (priority 100) — prompt rewriting/enhancement
2. **intent_evaluator** (priority 110) — evaluates request intent to guide routing
3. **token_compressor** (priority 120) — visual token compression with Feature Cache
4. **draft_generator** (priority 130) — draft-to-hires image/video generation
5. **gen_model_router** (priority 140) — generation-aware model routing
6. **cost_tracker** (priority 150) — per-group cost aggregation

### Path 3: Media Optimization Layer (V2)
Runs before LLM calls when multimodal content is detected:
- **Images**: resize, OCR (tesseract), caption (Vision model)
- **Video**: keyframe extraction, scene detection, audio transcription (faster-whisper)
- **Audio**: transcription, format conversion
- **Documents**: PDF/docx parsing, chunking, summarization

## Request Processing Flow (总分总架构)

所有 `/v1/chat/completions` 请求经 `RequestDispatcher.dispatch()`(dispatcher.py),总分总结构:

**总(共用前置 + 入口分流)**:
1. **共用前置**(C.3 决策 1,两管道共享,分流前跑):media_optimization → PII 检测。生成管道也过 PII(用户 prompt 里粘的邮箱/API key 会被脱敏)。
2. **分流** `classify_request(body, config_manager)` 只看模态/意图,**不看 model**:
   - 显式意图 `body.generation_intent == True` → generation
   - 模态推断:messages 含 image/audio/video 生成块 → generation
   - 模型名推断(仅非 auto):命中 generative 模型 → generation
   - 默认:understanding(含 `model=='auto'` 的纯文本请求)
3. **auto 不在入口解析** —— `model=='auto'` 原封传给 LiteLLMBridge,在管道末端按 pipeline_kind 解析(见下)。

**分(两条管道,各跑 PipelineEngine 插件链)**:
- **理解管道**(`PipelineEngine[pipeline_kind="understanding"]`,注册 7 插件:pii/cache/semantic/compress/rag/conv/media_optimizer):dispatch 共用前置已跑 pii/cache/semantic/media,engine 跑前用 `_skip_names` 再过滤掉 pii_detector/prompt_cache/semantic_cache/prompt_compress/media_optimizer → engine 实际只执行 **rag_retriever + conv_compressor**。→ 配额 → prompt_compress(已 inline)→ LiteLLM 出口 → 回填
- **生成管道**(`PipelineEngine[pipeline_kind="generation"]`,6 插件):ai_director → intent_evaluator → token_compressor → draft_generator → gen_model_router → cost_tracker → 配额 → LiteLLM 出口(不查理解缓存)

**总(LiteLLM 统一出口 + auto 末端解析)**:`LiteLLMBridge.completion()`/`completion_stream()` 是两条管道的共同出口,带 fallback 链(`fallback_chain` 列表)。`model=='auto'` 时 bridge 用注入的 `ModelRouterStrategy`(`set_auto_resolver`)按 pipeline_kind 选候选池(understanding→llm/mllm,generation→generative),complexity 评分选最优,结果写 `_meta.model_router`。「选哪个模型」的决策在管道末端,不在入口。

> ⚠️ **CircuitBreaker 未接入 LLM 调用路径**(2026-07-06 核实) — `litellm_bridge.py` 中无任何 `circuit_breaker` 引用,`.protect()`/`allow_request`/`record_*` 全仓零调用。`CircuitBreakerFactory` 的唯一消费者是 `routes.py`/`admin_routes.py` 读 `_breakers` 状态暴露给 `/metrics`/`/health`/admin。即 CircuitBreaker 当前是**纯观测基础设施,OPEN 状态不会真正熔断请求**。fallback 仅靠 `fallback_chain` 顺序重试。如需真正熔断,需在 `LiteLLMBridge.completion`/`_do_completion` 内调 `cb_factory.get(provider).protect(...)` 并在成功/失败时 record。

流式路径已对齐非流式行为:扣配额(`key_store.increment_usage`)、回填缓存(L1/L2/L3)、cost 用真实值。生成管道不查 prompt_cache(生成结果缓存语义复杂)。

## Three-Tier Cache

- **L1**: Process-local `cachetools.LRUCache` (maxsize=1000), <1ms, single entry ≤100KB
- **L2**: Redis hash with LZ4 compression, TTL configurable (~3600s), single entry ≤500KB
- **L3**: Qdrant vector similarity cache, embedding via `Qwen/Qwen3-Embedding-0.6B` (1024-dim), cosine ≥0.95, TTL ~86400s
  - Retrieve + Rerank two-stage: Qdrant top-K coarse retrieval → Lightweight/CrossEncoder reranking
  - Backfill strategy: L2 hit → backfill L1; L3 hit → backfill L1 only (not L2, since L3 is approximate); MISS → backfill L1+L2 + async L3 (if token_count ≥ 100)
  - Periodic cleanup scheduler (default 60min) removes expired auto-mode entries

## Security & Quotas

`KeyStore` stores API keys as Redis hashes with per-key limits:
- Daily token cap (default 1M)
- Monthly cost cap (default $50)
- RPM (requests per minute) sliding window (default 60)
- TPM (tokens per minute) sliding window (default 100K)
- Pub/Sub channel for multi-instance key sync
- Auto-reseed from config.yaml if Redis is empty

`PIIDetector` uses 3-pass detection: exclusion patterns → named fields → standalone patterns, with sanitize/reject/hash strategies.

## Metrics

Prometheus histogram for request duration, counters for cache hits/misses/tokens, gauges for active requests and circuit breaker states. Scraped by Prometheus at `/metrics` (no auth, rate-limited). Frontend parses Prometheus text format client-side via `parseMetrics()`.

## Key Files

| File | Purpose / 何时去这里 |
|------|---------|
| `config.yaml` | Single source of truth: server, auth, plugins, providers, embedding, observability, media_optimization, cache, circuit_breaker。**改任何运行参数、加 provider、调插件开关先改这里**(`hot_reload: true` 时热生效)。 |
| `config.yaml.template` | Full parameter documentation with comments。**改 config 字段前先看这里确认 schema**。 |
| `docker-compose.yml` | 6 services: gateway, control-panel, redis, qdrant, prometheus, grafana。**加服务/改端口/改环境变量去这里**。 |
| `aigateway-api/Dockerfile` | Python 3.12-slim;分层安装(apt→torch→requirements.txt→Qwen3 模型→源码),改源码秒级重建。**加 Python 依赖层/改构建步骤去这里**。 |
| `aigateway-api/requirements.txt` | gateway 全部 Python 依赖集中清单。**新增/升级 Python 依赖改此文件,不是 Dockerfile**。 |
| `Dockerfile.frontend` | Multi-stage: Node 20 Alpine builder(`npm ci`)→ Nginx Alpine serve。 |
| `.dockerignore` | 排除 .venv/node_modules/.git 等,构建上下文 < 10MB。 |
| `.env.example` / `.env.docker` | 入库模板:运行时配置 / BuildKit 开关;`.env` 本身 gitignored。**加环境变量先在 .env.example 建模板**。 |
| `docs/DB_SCHEMA.md` | Redis keys, Qdrant collections, PipelineContext structures。**改缓存 key/Redis 结构先核对此文档**。 |
| `docs/ARCHITECTURE_DIAGRAM.md` | 架构图。**看总分总/双入口结构去这里**。 |
| `docs/TEST_PLAN.md` | 测试计划。 |
| `aigateway-api/src/aigateway_api/dispatcher.py` | RequestDispatcher — 总分总入口:共用前置(media+PII)→ 分流 → 两条管道 → LiteLLM 出口。**改请求流程/分流逻辑/缓存回填先读它**。 |
| `aigateway-api/src/aigateway_api/openai_compat.py` | `/v1/chat/completions` 入口(调 dispatcher) + 辅助函数。**改 SSE 流式/响应组装/请求日志记录去这里**。 |
| `aigateway-api/src/aigateway_api/admin_routes.py` | API Key CRUD, quotas, plugin config, logs, RAG, L3 cache mgmt。**加 admin 接口去这里**。 |
| `aigateway-api/src/aigateway_api/main.py` | App factory, lifespan init, router mounting, 两条管道 Engine 实例。**加路由/改启动初始化/改 sys.path 去这里**。 |
| `aigateway-core/src/aigateway_core/pipeline.py` | PipelineEngine(按 pipeline_kind 装载) + 经典插件(PII/cache/semantic/compress)。**改插件链/加经典插件去这里**。 |
| `aigateway-core/src/aigateway_core/context.py` | PipelineContext — 共享请求状态。**改请求级上下文字段去这里**。 |
| `aigateway-core/src/aigateway_core/caching.py` | CacheManager: L1→L2→L3 + reranker。**改缓存命中/回填策略去这里**。 |
| `aigateway-core/src/aigateway_core/security.py` | KeyStore(配额/rate-limit) + PIIDetector(20+ 模式)。**改鉴权/配额/PII 脱敏去这里**。 |
| `aigateway-core/src/aigateway_core/litellm_bridge.py` | LiteLLM Router 多 provider + fallback(`fallback_chain`)。**改模型调用/fallback/auto 解析去这里**。注意:CircuitBreaker 未接入此文件(见 Known States)。 |
| `aigateway-core/src/aigateway_core/generation_optimization/` | 6 插件 8 策略的生成优化层。**改 ai_director/token_compressor/draft_generator 等去这里**。 |
| `aigateway-core/src/aigateway_core/media/` | Media Optimization Layer V2。**改图片/视频/音频/文档处理去这里**。 |
| `control-panel/src/pages/` | 9 个页面组件。**改控制台某页面 UI 去对应 .tsx**。 |
| `control-panel/src/api/client.ts` | VITE_API_BASE 前缀 fetch + Prometheus 文本解析。**加前端 API 调用去这里**。 |

## Development Commands

### Backend (local)

```bash
# Install packages in editable mode (order matters: core first)
cd aigateway-core && pip install -e .
cd ../aigateway-api && pip install -e .
cd ../aigateway-cli && pip install -e .

# Run API service (with auto-reload)
cd aigateway-api
uvicorn src.aigateway_api.main:create_app --factory --host 0.0.0.0 --port 8000 --reload

# Run CLI
aigateway chat
aigateway run --prompt "你好"
```

### Frontend (local)

```bash
cd control-panel
npm install
npm run dev        # Vite dev server on :5173
npm run build      # tsc -b && vite build
```

Vite dev proxy (in `vite.config.ts`) forwards `/aigateway/*` to `http://localhost:8000`.
Production uses nginx proxy at `/aigateway/*` → `http://gateway:8000/`.

### Docker Compose

```bash
# 构建加速:启用 BuildKit(并行层构建 + 增量上下文)
# .dockerignore 已排除 .venv/node_modules/.git,构建上下文 < 10MB
sudo DOCKER_BUILDKIT=1 docker compose up -d --build gateway   # 重建 gateway(改源码后秒级命中缓存)
sudo DOCKER_BUILDKIT=1 docker compose up -d --build control-panel
docker compose up -d          # Start all 6 services
docker compose down           # Stop everything
docker compose up -d --build  # Rebuild and restart
```

> 也可 `set -a && source .env.docker && set +a` 一次性导入 BuildKit 开关后省去 `DOCKER_BUILDKIT=1` 前缀。

Services:
- Gateway API: `http://localhost:8000`
- Control Panel: `http://localhost:3000`
- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3001` (admin/admin)

### Testing

```bash
# Run all tests
python -m pytest tests/ -v

# Run specific test file
python -m pytest tests/test_token_compressor_strategy.py -v

# Run with coverage
python -m pytest tests/ --cov=aigateway_core --cov=aigateway_api
```

Tests live in `/tests/` (25 files). No conftest.py or pytest.ini — tests run directly with `python -m pytest`. Note: `tests/test_template_routes.py` is a pre-existing flaky test — skip with `--ignore=tests/test_template_routes.py` when running the full suite. The environment uses `python3` (no `python` alias); use `python3 -m pytest ...`.

### Configuration

**配置优先级(高 → 低):**
1. 进程真实环境变量(docker-compose `environment:` 段 / shell `export`)— 最高
2. `.env` 文件(`python-dotenv` `load_dotenv(override=False)` 加载,不覆盖已存在的环境变量)— 运维常改项
3. `config.yaml` 明文值
4. 代码内 `_DEFAULT_CONFIG` 默认值 — 最低

- `.env`(gitignored,不入库)— 运行时变量;`.env.example` 是入库模板,列全所有可配置项。
- `config.yaml` — YAML config with `${ENV_VAR}` / `${VAR:-default}` interpolation. Provider API keys 等敏感值已改为 `${VAR}` 引用 `.env` 变量。
- `AI_GATEWAY_*` env vars override YAML sections (e.g., `AI_GATEWAY_REDIS_URL`);`load_dotenv()` 在 `main.py` / CLI `__main__.py` 入口最早处执行,先于 `ConfigManager` 读取。
- `AI_GATEWAY_GENERATION_OPTIMIZATION_*` env vars override generation_optimization section.
- `hot_reload: true` in config enables Watchdog file watcher for live config updates.
- Environment mode: `AI_GATEWAY_ENV=production` forces debug_mode=False, log_level≥INFO.

## Important Patterns

1. **App state via FastAPI lifespan** — All shared components (ConfigManager, KeyStore, CacheManager, LiteLLMBridge, PluginRegistry, CircuitBreakerFactory, MediaOptimizationPlugin, PromptTemplateManager) are initialized in `main.py`'s `lifespan()` context manager and stored on `app.state`. Route handlers read from `_get_app_state()`.

2. **Sys path manipulation** — `main.py` manually prepends `aigateway-core/src` to `sys.path` so imports work. The Dockerfile copies both packages into `/app/` so the prod path differs from local dev.

3. **Prometheus lazy init** — Metrics are created on first access via `_ensure_initialized()`. The histogram buckets for duration are: `[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0]`.

4. **Frontend parses Prometheus text** — `client.ts::parseMetrics()` converts Prometheus text format to JSON objects client-side. No backend endpoint for structured metrics.

5. **Dependencies via `requirements.txt`** — gateway 依赖集中在 `aigateway-api/requirements.txt`(合并自原 Dockerfile 的 6 个 pip 层 + `python-dotenv`)。新增/升级 Python 依赖改该文件,而非 Dockerfile。Local dev uses `pip install -e .` with no setup.py/pyproject.toml (editable installs work because packages are just directories under `src/`). torch 单独在 Dockerfile 前置层安装(CPU 版,最大最稳定)。

6. **`.env` for runtime config** — `.env`(gitignored)holds runtime/secrets; `.env.example` is the checked-in template. `python-dotenv` `load_dotenv(override=False)` runs at the very top of `main.py` and CLI `__main__.py`, before any config read. Priority: process env > `.env` > `config.yaml` > defaults. Production builds use `VITE_API_BASE=/aigateway` from `.env.production`.

7. **Build-accelerated Docker images** — `.dockerignore` excludes `.venv`(5GB+)/`node_modules`/`.git`/`dist`/`docs`, keeping build context < 10MB. BuildKit(`DOCKER_BUILDKIT=1`,见 `.env.docker`)enables parallel layer builds. gateway Dockerfile 分层顺序:apt → torch → `pip install -r requirements.txt` → Qwen3 模型预下载 → COPY 源码(最后),改源码只重建最后两层。Frontend 用 `npm ci`。

8. **Generation Optimization Config Hot-Reload** — `GenerationOptimizationConfigWatcher` registers with `ConfigManager.on_reload()` callbacks. Invalid field values fall back to previous valid values (never crash the service).

9. **Admin routes use file locking** — Config writes use `fcntl.flock()` to prevent concurrent write conflicts in multi-worker deployments.

10. **Two-layer plugin registration** — `_register_builtin_plugins()` handles the classic pipeline plugins (pii_detector, prompt_cache, etc.). `register_generation_optimization_plugins()` handles the newer generation optimization layer (ai_director, token_compressor, etc.). Both register into the same `PluginRegistry`.

11. **Embedding model caching** — L3 semantic cache uses module-level `_l3_model_cache` in `openai_compat.py` to avoid reloading the ~600MB Qwen3-Embedding-0.6B model per request.

## Architecture Decisions & Known States

- **Debug 开关体系(2026-07-05, PR2)** — `debug_mode` 总开关替换为 `config.yaml` 的 `debug:` 段。`DebugConfig`(`aigateway-core/src/aigateway_core/debug_config.py`)含 **6 字段**:`frontend/entry/cache/bridge`(4 大区)+ `plugins_enabled`(插件总开关)+ `per_plugin`(dict,11 插件),AND 逻辑(插件层需 plugins_enabled AND 单插件都开才发 kind=debug)。+ `DebugConfigWatcher` 走 `ConfigManager.on_reload()` 回调(签名 `Callable[[Dict], None]`,接收整个新 config dict),atomic swap 无锁读;`init_debug_config_watcher()` 进程级单例,main.py lifespan 启动。`TraceCollector.emit_debug(stage, name, duration, status, dimension, payload)` 查对应维度开关,关则静默,开则发 `kind=debug` 事件 + payload;dimension ∈ entry|cache|bridge|plugin(plugin 用 `is_plugin_debug` AND)。dispatcher `_emit_stage` 镜像 entry 维度;pipeline engine 成功路径镜像 plugin 维度(payload=input_summary 截断 500);CacheManager.get 各命中/MISS 路径发 cache 维度(key_hash+tier_hit);litellm_bridge.completion 正常返回路径发 bridge 维度(model+stream)。admin 接口:`POST /admin/plugins/{name}/debug`(fcntl.flock 写 per_plugin)、`GET /admin/config/debug`、`/admin/plugins-config` 响应每插件加 `debug` 字段(prompt_compress 返回 null,前端隐藏其 Debug 按钮)、`/admin/global-config` GET/PUT 读写整个 `debug` 段。`debug_mode` 字段在 config.py 仍保留(production 安全网),但语义已废弃;5xx detail 固定回显不再受其控制(PR1 已做)。开关总数:4 大区 + 1 插件总开关 + 11 插件 = 16。
- **控制台 UI 改造(2026-07-05, PR3;调试开关卡片 2026-07-06 迁移)** — `control-panel/src/pages/Plugins.tsx`: 双级分组渲染(外圈 pipeline_kind: understanding/generation, 内圈 getCategory: 缓存/安全/性能/路由/其他);每插件卡片新增 Debug 按钮(Bug icon),`debug===null`(prompt_compress)时隐藏;**"调试开关"卡片(5 维度 toggle: frontend/entry/cache/bridge/plugins_enabled)现位于此页"全局配置"区内**(2026-07-06 从 Config.tsx 迁入,Config.tsx 已移除该卡片及 debug 相关 import/state)。`Logs.tsx`: trace detail modal 用 `events` 瀑布流(含 kind=stage/plugin/debug 三类事件,带颜色节点 + payload 折叠),替代旧 plugin_trace 列表 + 耗时分布条。`client.ts`: 新增 `DebugConfig`/`TraceEvent` 接口 + `getDebugConfig`/`setPluginDebug`/`updateDebugSection` API 函数。
- **全链路 trace_id + TraceEvent 通道(2026-07-05, PR1)** — 新增 `TraceEvent`/`TraceCollector`(`aigateway-core/src/aigateway_core/trace_event.py`)+ ASGI `TraceMiddleware`(`aigateway-api/src/aigateway_api/trace_middleware.py`)。一次请求由中间件生成/透传 trace_id 写 `request.state.trace_id`,`PipelineContext.trace_id` 改必传(删默认 factory);5 个 ctx 构造点 + `_record_request_log` 复用 `request.state.trace_id`。engine 循环 + dispatcher 内联 + `_run_engine_filtered` + 6 gen-opt 插件所有埋点统一 `collector.emit(TraceEvent(kind=stage|plugin))`;`add_plugin_trace` 保留为兼容 shim(dual-write list + emit)。`model_router` 空壳彻底退役(删类 + 删注册)。logger `ContextInjectProcessor` 优先读 `TraceCollector.current().trace_id`,所有 stdlib `logger.debug/info` 自动带 trace_id。TraceMiddleware 挂在 `create_app` 末尾(RateLimiter 之后,Starlette last-added = outermost),是真正最外层。5xx 错误 detail 固定回显 redacted 内容(不再受 debug_mode 控制),新增 `@app.exception_handler(Exception)` 兜底处理器覆盖所有 5xx(GatewayError/HTTPException 之外的异常也返回统一结构 + X-Request-ID);`_is_debug_mode()` 已删,`debug_mode`-强制-DEBUG-日志级别的逻辑已删。`/admin/trace/{id}` 返回 events 数组(fallback 旧 ZSET);Redis 新 key `aigateway:trace:{trace_id}`(hash,TTL 7 天)。
- **总分总架构(2026-07)** — 所有 `/v1/chat/completions` 请求经 `RequestDispatcher`(dispatcher.py):共用前置(media+PII)→ 分流(只看模态/意图)→ 两条管道各由 `PipelineEngine` 驱动插件链 → LiteLLM 统一出口。`PipelineEngine`/`PluginRegistry`/`Plugin`/`PipelineContext` 四类加了 `pipeline_kind` 维度。`openai_compat.py` 的手工串行链已删除,辅助函数(`_apply_*`/`_record_request_log` 等)保留供 dispatcher 复用。
- **auto 模型解析下沉到 bridge(2026-07-04)** — `model=='auto'` 不再在 dispatcher 入口解析。`classify_request` 只看模态/意图,auto 请求按模态分流后原封传给 `LiteLLMBridge`。bridge 通过 `set_auto_resolver(ModelRouterStrategy)` 注入解析器,在管道末端按 `pipeline_kind` 选候选池(understanding→llm/mllm,generation→generative),complexity 评分选最优,结果写 `_meta.model_router`。理由:让「选哪个模型」的决策发生在管道链末端(可拿到 PII/压缩/RAG 信号),而非入口越权决定。
- **共用前置(C.3 决策 1,2026-07-04)** — media_optimization + PII 在 `dispatch()` 里、`classify_request` 之前跑,两管道共享。生成管道也过 PII(用户 prompt 里粘的邮箱/API key 会被脱敏)。auto 解析不在共用前置(属路由决策,见上一条)。
- **model_router 空壳已彻底删除**(2026-07-06 核实) — `ModelRouterPlugin` 类与注册均已从 `pipeline.py` 移除,理解管道 `_skip_names` 里也不再含它(根本没注册)。真路由由 bridge 的 auto 解析(注入 ModelRouterStrategy)承担,`classify_request` 只做模态分流。上方 Path 1 列表里的 "model_router" 条目为遗留描述,实际不存在。
- **GenerationPipeline(media/generation.py)deprecated** — 孤儿代码 0 生产引用,生成管道由 generation_optimization 6 插件链承担。
- **AIDirectorStrategy 延迟绑定 litellm_bridge** — `register_generation_optimization_plugins` 在 bridge 建好前跑,main.py 在 bridge 初始化后从 registry 取 strategy 单例注入 `_litellm_bridge`。
- **热重载完整闭环** — admin PUT(global-config/plugins-config)写文件后调 `atomic_swap` → `_notify_reload` → main.py 的 `_on_config_reload` 回调同步 plugins.enabled 并重建两条管道 Engine。
- **contextvar 日志上下文** — `ContextInjectProcessor` 用 `contextvars.ContextVar` 隔离并发请求的 trace_id(原类级共享 dict 有并发覆盖 bug)。
- **prompt_compress** is now a real implementation using LLMLingua-2 (multilingual). `device: cpu|cuda` controls runtime; `device_map` auto-set for GPU. `compression_ratio`/`target_token` control output size.
- **rag_retriever** & **conv_compressor** are default-enabled with local fallback behavior (no external service strictly required to start, though Qdrant is needed for full RAG retrieval).
- **L3 集合 404 容错**(2026-07-05) — `qdrant_client.py` 的 `search`/`retrieve` 路径在 Qdrant 返回 404(集合尚未创建/被清空)时视为 miss 返回 `None`,而非 `raise_for_status()` 冒泡成 5xx。集合由首次 `set_l3` 写入时懒创建。这避免了首次部署或 Qdrant 数据清空后,语义缓存查找让整个请求失败。
- **debug_mode**(2026-07-05 起废弃单开关语义) — `debug_mode` 现只用于向后兼容;`_is_debug_mode()` 已删,不再强制 DEBUG 日志级别,不再控制 5xx detail 回显。`AI_GATEWAY_ENV=production` 仍强制 `debug_mode=False` + `log_level≥INFO` 作为生产安全网(config.py 中)。PR2 已落地 5 维度开关(见上一条),debug_mode 字段仅 config.py production 安全网保留。
- **TokenCompressorStrategy** uses deterministic hash-based feature vectors as placeholders — actual ML inference (CLIP/ViT segmentation + feature extraction) is planned for future integration.
- **Media Optimization Layer** handles OCR, video keyframes, audio transcription, document parsing — configurable per-media-type in `config.yaml`.
- **Single worker architecture** — `workers: 1` controlled by Dockerfile CMD. The `workers` config parameter in config.yaml is deprecated (removed by recent commit).
- **Per-model `base_url` override** — providers like Agnes route text/image/video generation through different API endpoints. Each model entry in `providers.<name>.model_grouper[].models[]` may set an optional `base_url`; if omitted or empty, it inherits the provider-level `base_url`. Fallback models always use the provider-level URL (never inherit a primary model's custom URL). Implemented in `_build_model_list()` at init time — LiteLLM Router treats each `model_list` entry independently, so no runtime/call-path changes are needed.
- **理解管道 engine 实际只跑 2 插件**(2026-07-06 核实) — 注册 7 个(pii/cache/semantic/compress/media/rag/conv),dispatcher `_skip_names`(dispatcher.py:339)过滤掉 5 个(pii_detector/prompt_cache/semantic_cache/prompt_compress/media_optimizer),engine 实际执行只剩 **rag_retriever + conv_compressor**(media 默认关闭)。生成管道相反,`_dispatch_generation` 不设 skip,6 个 gen-opt 插件全跑(dispatcher.py:531)。CLAUDE.md "8 插件"为旧描述,已改"注册 7 个"。⚠️ **已修复 bug**:`_skip_names` 曾误写 `media_optimization`(注册名实为 `media_optimizer`),导致 skip 失效、media 开启时被共用前置和 engine 双跑;2026-07-06 已改正(dispatcher.py `_skip_names`)。
- **prompt_compress / PIIDetectorPlugin 双实例化** — registry 内一份(理解管道被 `_skip_names` 跳过)+ `app.state` 单独构造一份(`main.py:399/440`,实际执行)。Inline 集成模式副产品,非 bug。
- **前端 useAuth / usePoll 是死代码**(2026-07-06 核实) — `control-panel/src/hooks/useAuth.ts` 和 `usePoll.ts` 零外部引用。实际鉴权在 `Plugins.tsx` 内联实现(直接调 `saveApiKey/getSavedApiKey`),轮询由 Overview/Costs/Cache 各自 `setInterval` 自管。清理这两个 hook 或抽成 Auth 组件 + 启用 usePoll 是改进点。
- **6 个前端 API 函数零调用方** — `client.ts` 的 `createChatCompletion`/`createChatCompletionStream`/`listModels`/`createEmbeddings`/`getQuota`/`getMetricsJson` 无页面调用,预留给入口 B(chat agent,spec `docs/superpowers/specs/2026-07-05-control-panel-chat-agent-design.md`)。`getMetricsJson` 与 `getMetricsText`+`parseMetrics` 客户端解析路径功能重叠且闲置。
- **前端鉴权隐式非集中** — 无登录页/路由守卫/全局 Auth Provider。`ensureAuthHeaders()`(`client.ts:31`)默默从 localStorage 读 key 注入。401 时除 `Plugins.tsx`(有重试 UI)外,其他页面静默 catch 显示空数据。未配 key 时除 Plugins/Overview(health 无鉴权)外页面"空白无提示"。

## Workflow Rules (post-task actions)

After every code-changing task is complete and verified:

### 1. Auto-commit with conflict confirmation
- Stage and commit all changes from the task with a clear conventional-commit message (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`) summarizing what changed.
- If `git commit` or `git add` produces a merge conflict (e.g. rebase/merge needed, or uncommitted changes from another branch clash), **do NOT force-resolve**. Stop, surface the conflict details, and ask the user to confirm how to proceed before continuing.
- Commits use the configured git identity (Gateway2). Do not push unless the user explicitly asks — see rule 4 for the push-after-merge policy.

### 2. Rebuild Docker image when required
- Determine whether the change requires a Docker rebuild to take effect:
  - **Requires rebuild**: any change to backend Python source under `aigateway-api/` or `aigateway-core/`, Dockerfile changes, dependency additions, or `config.yaml` structural changes baked into the image.
  - **Does NOT require rebuild** (live config): edits to `config.yaml` values when `hot_reload: true` (Watchdog picks them up at runtime), frontend-only changes during `npm run dev` (Vite HMR), or pure documentation.
- When a rebuild is required, rebuild the affected service(s) and verify the change took effect:
  ```bash
  docker compose up -d --build gateway        # backend changes
  docker compose up -d --build control-panel  # frontend changes
  # then verify, e.g.:
  curl -s http://localhost:8000/health
  docker compose logs --tail=50 gateway | grep -i error
  ```
- Report the rebuild + verification result alongside the commit. If Docker is not running or rebuild fails, surface the error rather than skipping silently.

### 3. Keep CLAUDE.md current
- Maintain `CLAUDE.md` as a living document. After any task that changes architecture, adds/removes a major component, alters config schema, changes commands, or shifts a known-state item, update the corresponding section in `CLAUDE.md` in the same task.
- Periodically (and at least when the architecture overview or pipeline flow no longer matches the code), refresh: scan `aigateway-core/src/`, `aigateway-api/src/`, and `control-panel/src/` for new/removed modules and reconcile the "Architecture at a Glance" diagram, "Plugin Pipeline Flow", "Important Patterns", and "Architecture Decisions & Known States" sections.
- Do not let CLAUDE.md drift from reality — outdated guidance misleads future sessions more than missing guidance.

### 4. Careful merge with conflict-of-function review, then push to remote
When merging code (e.g. feature branch → `main`, or integrating another branch's changes):
- **Before resolving conflicts, check for functional conflicts or overrides** — not just textual `<<<<<<<` markers. Two branches may both apply cleanly yet implement the same feature in incompatible ways, or one branch's change may silently override/revert the other's intended behavior (e.g. both editing the same function/section in `config.yaml`, both adding a plugin with the same name, both modifying the same route handler). Examine the merged result holistically: does every feature from both sides still work as intended, or did one side's edit negate the other's?
- **Evaluate which to keep before asking.** When a functional conflict exists, first form your own assessment: which version is correct / more complete / better aligned with current architecture, and why. Present that recommendation along with the trade-off, then ask the user to confirm which side to keep — do **not** silently pick one, and do **not** reflexively ask without a recommendation.
- **Never force-resolve blindly.** If unsure, surface the specific conflict (file, lines, both sides' intent) and ask.
- **Push to remote GitHub promptly after merging into `main`.** Once a merge lands on `main` (and any required Docker rebuild + verification per rule 2 is done), push to the remote GitHub repo without waiting to be asked. This is the explicit exception to rule 1's "do not push unless asked" — merge-to-main triggers a push. If the push is rejected (non-fast-forward), pull/rebase first; if conflicts arise during that rebase, fall back to the conflict-confirmation policy above before continuing.

### 5. Token-efficient navigation (避免全量扫描)

省 token 的核心:用「导航 + 精准读」替代「全量扫描」。每次任务只读真正需要的 1-3 个文件,不整库翻阅。

- **优先用 LSP 查符号,而非 grep 文本匹配。** 找定义/引用/类型/符号时,优先用 LSP 工具(`goToDefinition`/`findReferences`/`hover`/`documentSymbol`/`workspaceSymbol`/`goToImplementation`/`prepareCallHierarchy`+incoming/outgoing)——语言感知、跟 import/re-export、知类型,比 grep 精准且不产生假阳性(注释/影子名/子串)。覆盖:`.ts/.tsx/.js/.jsx` → typescript-lsp,`.py/.pyi` → pyright-lsp,`.go` → gopls-lsp。**若 LSP 工具报 `No LSP server available`,说明本环境未装/未生效**,按 `README.md` 的「开发 → 配置 Claude Code 的 LSP 代码智能」五步装好(pyright/typescript-language-server 二进制 + `claude plugin install` + `ENABLE_LSP_TOOL=1` + `claude plugin details` 验证 + 重启会话),再继续。仅当 LSP 不适用时才退回 grep:搜字符串/正则/文本模式(非代码符号)、查注释/配置/文档内容、LSP 不覆盖的文件类型。
- **先查 CLAUDE.md 定位,再读具体文件。** 接到任务先扫 CLAUDE.md 的 `Key Files` 表(带"何时去这里"触发词)、`Architecture at a Glance` 图、`Architecture Decisions & Known States` 段,据此锁定目标文件,再 `Read` 该文件。禁止为"了解架构"而全量读 `repomix-output.md` 或 `cat` 整个目录。
- **LSP 优先;grep/glob 作退路,Read 带 offset/limit。** 定位代码符号先 `LSP`(见上条);LSP 不适用时用 `Grep "pattern"` 命中文件。命中后 `Read` 该文件相关区间(传 `offset`/`limit`),不要无参 Read 整个大文件再翻。例如找某插件:先 `workspaceSymbol "XPlugin"`,失败再 `Grep "class.*Plugin"` → 命中文件 → Read 该类定义 ±50 行。
- **修 bug 从堆栈/trace_id 直达。** 用报错堆栈或 trace_id 定位 `file:line`,Read 该文件 ±50 行即可,不要扫整库。trace_id 可查 `/admin/trace/{id}` 拿 events 数组。
- **架构调整先画影响面。** 改动某符号/接口前,先 `LSP findReferences` 列出所有调用方;LSP 不适用时再用 `Grep "from.*import|import "`。确认影响范围后再动手,绝不"先通读全部相关文件再改"。
- **多文件广度搜索派 subagent。** 涉及多文件的广度搜索(找所有调用点/所有实现某接口的类/跨包追踪数据流),优先派 `Explore` 或 `general-purpose` subagent 去扫,主上下文只接收其结论(几百 token),不把几十个文件原文灌进来。
- **`repomix-output.md` 仅作目录索引参考,禁止整文件读入。** 需要时只 `Grep` 其中的 directoryStructure 段定位文件路径,具体内容去读源文件。
- **Config/契约类改动先核对应处文档。** 改 API 字段查 `openai_compat.py`/`dispatcher.py` 的请求响应组装(旧 `docs/API_CONTRACT.md` 已删),改缓存 key 查 `docs/DB_SCHEMA.md`,改 config 字段查 `config.yaml.template`,避免改出不一致。
