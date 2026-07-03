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
│  ├── main.py         │  App factory, lifespan init, router mounting
│  ├── openai_compat.py│  POST /v1/chat/completions, GET /v1/models, /v1/embeddings
│  ├── admin_routes.py │  API Key CRUD, quotas, plugin config, logs, RAG, L3 cache mgmt
│  ├── auth_middleware.py│ Bearer/x-api-key validation via KeyStore
│  ├── streaming.py    │  SSE generator + cached stream simulation
│  ├── routes.py       │  GET /metrics, GET /health
│  ├── draft_routes.py │  Draft-to-HiRes generation endpoints
│  ├── template_routes.py│ Prompt template management endpoints
│  └── rate_limiter.py │  IP-based rate limiting middleware
├── aigateway-core     │  Shared library (imported by API + CLI)
│  ├── pipeline.py     │  Async plugin engine + 5 built-in plugins (PII, cache, semantic_cache, model_router, prompt_compress)
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

## Plugin Pipeline Flow

Request enters FastAPI → auth_middleware validates API key → two parallel processing paths:

### Path 1: Built-in Pipeline (pipeline.py)
1. **pii_detector** — scan for 20+ PII patterns (email, phone, credit card, Chinese ID, passwords, API keys, connection strings), sanitize/reject/hash
2. **prompt_cache** — exact-match cache (L1 → L2 → L3)
3. **semantic_cache** — vector similarity on missed prompts (Qdrant, cosine ≥ 0.95)
4. **model_router** — select best provider/model based on cost/speed/quality strategy
5. **prompt_compress** — placeholder (records original length, no actual compression yet)

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

## Request Processing Flow (openai_compat.py)

Both stream and non-stream paths follow:
1. Media Optimization (if multimodal) → 2. Cache lookup (L1→L2→L3 with embedding) → 3. Quota check → 4. LiteLLM Bridge completion → 5. Usage recording + cache backfill

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

| File | Purpose |
|------|---------|
| `config.yaml` | Single source of truth: server, auth, plugins, providers, embedding, observability, media_optimization, cache, circuit_breaker |
| `config.yaml.template` | Full parameter documentation with comments |
| `docker-compose.yml` | 6 services: gateway, control-panel, redis, qdrant, prometheus, grafana |
| `aigateway-api/Dockerfile` | Python 3.12-slim, installs all deps inline, pre-caches Qwen3-Embedding model |
| `Dockerfile.frontend` | Multi-stage: Node 20 Alpine builder → Nginx Alpine serve |
| `docs/API_CONTRACT.md` | Request/response schemas, error formats |
| `docs/TECH_SPEC.md` | Technology choices, config schema |
| `docs/DB_SCHEMA.md` | Redis keys, Qdrant collections, PipelineContext structures |

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
docker compose up -d          # Start all 6 services
docker compose down           # Stop everything
docker compose up -d --build  # Rebuild and restart
```

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

Tests live in `/tests/` (22 files). No conftest.py or pytest.ini — tests run directly with `python -m pytest`.

### Configuration

- `config.yaml` — YAML config with `${ENV_VAR}` interpolation
- `AI_GATEWAY_*` env vars override YAML sections (e.g., `AI_GATEWAY_REDIS_URL`)
- `AI_GATEWAY_GENERATION_OPTIMIZATION_*` env vars override generation_optimization section
- `hot_reload: true` in config enables Watchdog file watcher for live config updates
- Environment mode: `AI_GATEWAY_ENV=production` forces debug_mode=False, log_level≥INFO

## Important Patterns

1. **App state via FastAPI lifespan** — All shared components (ConfigManager, KeyStore, CacheManager, LiteLLMBridge, PluginRegistry, CircuitBreakerFactory, MediaOptimizationPlugin, PromptTemplateManager) are initialized in `main.py`'s `lifespan()` context manager and stored on `app.state`. Route handlers read from `_get_app_state()`.

2. **Sys path manipulation** — `main.py` manually prepends `aigateway-core/src` to `sys.path` so imports work. The Dockerfile copies both packages into `/app/` so the prod path differs from local dev.

3. **Prometheus lazy init** — Metrics are created on first access via `_ensure_initialized()`. The histogram buckets for duration are: `[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0]`.

4. **Frontend parses Prometheus text** — `client.ts::parseMetrics()` converts Prometheus text format to JSON objects client-side. No backend endpoint for structured metrics.

5. **No pyproject.toml or requirements.txt** — Dependencies are installed inline in Dockerfiles via `pip install`. Local dev uses `pip install -e .` with no setup.py/pyproject.toml (editable installs work because packages are just directories under `src/`).

6. **`.env` is gitignored** — Copy `.env.example` to `.env` for local development. Production builds use `VITE_API_BASE=/aigateway` from `.env.production`.

7. **Generation Optimization Config Hot-Reload** — `GenerationOptimizationConfigWatcher` registers with `ConfigManager.on_reload()` callbacks. Invalid field values fall back to previous valid values (never crash the service).

8. **Admin routes use file locking** — Config writes use `fcntl.flock()` to prevent concurrent write conflicts in multi-worker deployments.

9. **Two-layer plugin registration** — `_register_builtin_plugins()` handles the classic pipeline plugins (pii_detector, prompt_cache, etc.). `register_generation_optimization_plugins()` handles the newer generation optimization layer (ai_director, token_compressor, etc.). Both register into the same `PluginRegistry`.

10. **Embedding model caching** — L3 semantic cache uses module-level `_l3_model_cache` in `openai_compat.py` to avoid reloading the ~600MB Qwen3-Embedding-0.6B model per request.

## Architecture Decisions & Known States

- **prompt_compress** is a placeholder — records original length but does not actually compress (marked TODO in code). Real implementation would integrate LangChain-style conversation summarization.
- **TokenCompressorStrategy** uses deterministic hash-based feature vectors as placeholders — actual ML inference (CLIP/ViT segmentation + feature extraction) is planned for future integration.
- **Media Optimization Layer** is the newest major feature (V2), handles OCR, video keyframes, audio transcription, document parsing — configurable per-media-type in `config.yaml`.
- **Single worker architecture** — `workers: 1` controlled by Dockerfile CMD. The `workers` config parameter in config.yaml is deprecated (removed by recent commit).
