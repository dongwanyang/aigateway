# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI Gateway is an OpenAI-compatible proxy that sits between clients and LLM providers, adding token optimization, multi-tier caching, model routing, PII sanitization, circuit breaking, and cost tracking. Clients change one `base_url` to route through the gateway with zero code changes.

## Architecture at a Glance

```
Client (OpenAI SDK / CLI / IDE)
        │
        ▼
┌─────────────────────┐
│  aigateway-api       │  FastAPI + Uvicorn (:8000)
│  ├── main.py         │  App factory, lifespan init, router mounting
│  ├── openai_compat.py│  POST /v1/chat/completions, GET /v1/models
│  ├── admin_routes.py │  API Key CRUD, quotas, plugin config, logs
│  ├── auth_middleware.py│ Bearer/x-api-key validation via KeyStore
│  ├── streaming.py    │  SSE generator + cached stream simulation
│  └── routes.py       │  GET /metrics, GET /health
├── aigateway-core     │  Shared library (imported by API + CLI)
│  ├── pipeline.py     │  Async plugin engine with dependency resolution
│  ├── plugin_registry.py│  Registration, topological sort, lifecycle
│  ├── context.py      │  PipelineContext — shared request state
│  ├── caching.py      │  CacheManager: L1(LRU) → L2(Redis+LZ4) → L3(Qdrant)
│  ├── security.py     │  KeyStore (Redis-backed quotas/rate-limits), PIIDetector
│  ├── litellm_bridge.py│  Wraps LiteLLM Router for multi-provider calls
│  ├── circuit_breaker.py│  Per-provider CLOSED/OPEN/HALF-OPEN state machine
│  ├── config.py       │  YAML loader, env var overrides, watchdog hot-reload
│  ├── metrics.py      │  Prometheus counters/histograms/gauges
│  ├── tracing.py      │  OpenTelemetry trace integration
│  ├── logger.py       │  Structured JSON logging via structlog
│  ├── redis_client.py │  Async Redis connection pool
│  ├── qdrant_client.py│  Async Qdrant HTTP client
│  └── exceptions.py   │  GatewayError → AuthError / QuotaExceededError / CircuitBreakerOpenError
├── aigateway-cli      │  CLI tool (aigateway chat / aigateway run)
│  ├── __main__.py     │  argparse entry point
│  ├── chat.py         │  Interactive REPL session
│  ├── run.py          │  Single-shot request
│  └── session.py      │  Named session persistence
└── control-panel      │  React SPA (Vite + TypeScript + TailwindCSS)
    ├── src/App.tsx     │  React Router: /, /plugins, /costs, /quotas, /cache, /logs
    ├── src/api/client.ts│ VITE_API_BASE-prefixed fetch calls, Prometheus text parser
    └── nginx.conf      │ Proxies /aigateway/ → gateway:8000/
```

### Plugin Pipeline Flow

Request enters FastAPI → auth_middleware validates API key → pipeline executes plugins in dependency order:

1. **pii_detector** — scan for 20+ PII patterns (email, phone, credit card, Chinese ID, passwords, API keys), sanitize/reject/hash
2. **prompt_cache** — exact-match cache (L1 → L2 → L3)
3. **semantic_cache** — vector similarity on missed prompts (Qdrant, cosine ≥ 0.95)
4. **model_router** — select best provider/model based on cost/speed/quality strategy, fallback chain
5. **prompt_compress** — placeholder for future compression

Short-circuit via `ctx.should_stop = True` at any stage. Non-critical plugin failures are fail-open.

### Three-Tier Cache

- **L1**: Process-local `cachetools.LRUCache` (maxsize=1000), <1ms
- **L2**: Redis hash with LZ4 compression, TTL configurable (~3600s)
- **L3**: Qdrant vector similarity cache, embedding via `sentence-transformers`, TTL ~86400s

### Security & Quotas

`KeyStore` stores API keys as Redis hashes with per-key limits:
- Daily token cap
- Monthly cost cap (USD)
- RPM (requests per minute) sliding window
- TPM (tokens per minute) sliding window

### Metrics

Prometheus histogram for request duration, counters for cache hits/misses/tokens, gauges for active requests and circuit breaker states. Scraped by Prometheus at `/metrics` (no auth).

## Key Files

| File | Purpose |
|------|---------|
| `config.yaml` | Single source of truth: server, auth, plugins, providers, embedding, observability |
| `docker-compose.yml` | 6 services: gateway, control-panel, redis, qdrant, prometheus, grafana |
| `aigateway-api/Dockerfile` | Python 3.12-slim, installs all deps, copies core+api, runs uvicorn |
| `Dockerfile.frontend` | Multi-stage: Node builder → Nginx serve |
| `docs/API_CONTRACT.md` | Request/response schemas, error formats |
| `docs/TECH_SPEC.md` | Technology choices, config schema |
| `docs/DB_SCHEMA.md` | Redis keys, Qdrant collections |

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

### Configuration

- `config.yaml` — YAML config with `${ENV_VAR}` interpolation
- `AI_GATEWAY_*` env vars override YAML sections (e.g., `AI_GATEWAY_REDIS_URL`)
- `hot_reload: true` in config enables Watchdog file watcher for live config updates

## Important Patterns

1. **App state via FastAPI lifespan** — All shared components (ConfigManager, KeyStore, CacheManager, LiteLLMBridge, PluginRegistry) are initialized in `main.py`'s `lifespan()` context manager and stored on `app.state`. Route handlers read from `_get_app_state()`.

2. **Sys path manipulation** — `main.py` manually prepends `aigateway-core/src` to `sys.path` so imports work. The Dockerfile copies both packages into `/app/` so the prod path differs from local dev.

3. **Prometheus lazy init** — Metrics are created on first access via `_ensure_initialized()`. The histogram buckets for duration are: `[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0]`.

4. **Frontend parses Prometheus text** — `client.ts::parseMetrics()` converts Prometheus text format to JSON objects client-side. No backend endpoint for structured metrics.

5. **No pyproject.toml or requirements.txt** — Dependencies are installed inline in Dockerfiles via `pip install`. Local dev uses `pip install -e .` with no setup.py/pyproject.toml (editable installs work because packages are just directories under `src/`).

6. **`.env` is gitignored** — Copy `.env.example` to `.env` for local development. Production builds use `VITE_API_BASE=/aigateway` from `.env.production`.
