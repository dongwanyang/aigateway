# CLAUDE.md

Guidance for Claude Code when working in this repo. Keep terse — see rule "Trim CLAUDE.md" below.

## Project Overview

AI Gateway: OpenAI-compatible proxy in front of LLM providers. Adds token optimization, tiered caching, model routing, PII scrubbing, cost tracking. Clients only change `OPENAI_BASE_URL`.

Three surfaces:
1. **API Gateway** — FastAPI/Uvicorn on `:8000` (`aigateway-api`)
2. **CLI** — `aigateway chat` / `aigateway run` (`aigateway-cli`)
3. **Control Panel** — React SPA on `:3000` (`control-panel`)

## Architecture

```
Client → aigateway-api (FastAPI :8000)
         └── RequestDispatcher (dispatcher.py) — 总分总 orchestrator
             1. Shared prefix:    media_optimization → PII
             2. classify_request: understanding | generation (by modality/intent, NOT model name)
             3. PipelineEngine[kind]:
                - understanding: rag_retriever + conv_compressor (other 5 skipped, ran in prefix)
                - generation:    ai_director → intent_evaluator → token_compressor
                                 → draft_generator → gen_model_router → cost_tracker
             4. Quota check → LiteLLMBridge.completion() → response
         Cache: L1 (in-process LRU) → L2 (Redis+LZ4) → L3 (Qdrant vector, cosine≥0.95)
         Auth: KeyStore (Redis-backed) via auth_middleware
```

**Two entry points** (agent identity, not just proxy):
- Entry A (present): `/v1/chat/completions` — OpenAI JSON/SSE for machines (SDK/CLI/IDE).
- Entry B (planned, spec `docs/superpowers/specs/2026-07-05-control-panel-chat-agent-design.md`): `/admin/agent/chat` — SSE for humans in Control Panel, adds AgentLoop (tool calling + HITL). Both share dispatcher / pipelines / LiteLLM exit.

**auto model resolution** lives inside `LiteLLMBridge` (via injected `ModelRouterStrategy.set_auto_resolver`), not at dispatch entry — decision uses `pipeline_kind` and post-pipeline signals (PII/compress/RAG). `classify_request` only routes by modality.

**Circuit breaking** is delegated to LiteLLM Router built-in cooldown (`allowed_fails` + `cooldown_time`). `ProviderCooldownTracker` mirrors state via `litellm._async_success/failure_callback` for `/metrics` and `/admin/metrics-json`. No custom breaker; no HALF-OPEN. Config keys `circuit_breaker.failure_threshold` / `recovery_timeout` are aliases.

## Package Layout

```
aigateway-api/src/aigateway_api/    FastAPI app
  main.py               App factory, lifespan (init state, build both PipelineEngines)
  dispatcher.py         RequestDispatcher — the entry orchestrator
  openai_compat.py      /v1/chat/completions handler + helpers (_apply_*/_record_request_log)
  admin_routes.py       API key CRUD, quotas, plugin config, logs, RAG, L3 cache mgmt
  streaming.py          SSE generator (also simulates streams from cache)
  routes.py             /health, /metrics
  auth_middleware.py    Bearer/x-api-key validation
  rate_limiter.py       IP rate limit
  trace_middleware.py   Generates/propagates trace_id → request.state
  draft_routes.py, template_routes.py

aigateway-core/src/aigateway_core/  Shared library
  pipeline.py           PipelineEngine (kind-aware) + classic plugins (pii/cache/semantic/compress/rag/conv/media)
  plugin_registry.py    Registration, topological sort, lifecycle
  context.py            PipelineContext (trace_id is required)
  caching.py            CacheManager L1→L2→L3, cache-key v2, rerankers
  security.py           KeyStore + PIIDetector (20+ patterns, 3-pass)
  litellm_bridge.py     LiteLLM Router wrapper + fallback + cooldown tracker + auto resolver
  config.py             YAML loader + env override + hot-reload (Watchdog)
  debug_config.py       DebugConfig + hot-reload watcher (16 switches)
  trace_event.py        TraceEvent / TraceCollector (contextvar)
  logger.py, metrics.py, tracing.py, redis_client.py, qdrant_client.py, exceptions.py
  generation_optimization/  6 plugins + 8 strategies (ai_director/intent_evaluator/
                            token_compressor/draft_generator/gen_model_router/cost_tracker)
  media/                Media Optimization V2: plugin, mol, cache, pipelines/{Image,Video,Audio,Document}

aigateway-cli/src/aigateway_cli/    __main__, chat, run, session
control-panel/src/                  App.tsx (routes), api/client.ts, pages/ (9), components/, hooks/
```

## Cache Key v2 (2026-07-06)

L2 prefix `aigateway:cache:v2:*`. v1 keys expire naturally, not purged.

```
key = SHA-256("v2" | tenant_id | pipeline_kind | model_family | temp_bucket | mt_bucket
              [ | u=user_id if scope=private ] | normalized_prompt)
```

- **model_family**: strips date snapshot (`gpt-4o-2024-08-06` → `gpt-4o`); `auto` kept literal. New snapshots don't bust cache.
- **temp_bucket**: `exact_zero` (≤0.05) / `det` (≤0.3) / `bal` (≤0.9) / `cre` (>0.9).
- **mt_bucket**: rounded up to `le_256/512/1024/2048/4096/8192/16384`; `None/0` → `any`.
- **top_p**: ignored (nearly always 1.0 in practice).
- **cache_scope**: header `X-Cache-Scope` > PII-forced `private` > default `shared` (see `dispatcher._resolve_cache_scope`).
- **normalized_prompt**: system + last 3 turns only (`dispatcher._extract_cacheable_context`), NFKC + whitespace collapse (`caching._normalize_prompt`).
- **metrics**: `gateway_cache_hits_total{tier}` / `gateway_cache_misses_total` counted at dispatcher (fixed a v1 blind spot).
- Tests: `tests/test_cache_key_v2.py`.

## Three-Tier Cache

| Tier | Store              | Latency | Size cap    | TTL     |
|------|--------------------|---------|-------------|---------|
| L1   | `cachetools.LRU`   | <1ms    | ≤100KB/entry, 1000 entries | in-process |
| L2   | Redis + LZ4        | few ms  | ≤500KB/entry | ~3600s |
| L3   | Qdrant (Qwen3-Embedding-0.6B 1024-dim, cosine ≥0.95) | ~50ms | — | ~86400s |

Backfill: L2 hit → L1; L3 hit → L1 only (approximate); MISS → L1+L2 + async L3 (if token_count ≥100). L3 has retrieve→rerank two stage. `qdrant_client.search/retrieve` treats 404 as miss (collection lazy-created on first `set_l3`).

## Security & Quotas

`KeyStore` — Redis hash per key. Per-key: daily tokens (default 1M), monthly cost ($50), RPM (60), TPM (100K). Pub/Sub sync across instances. Auto-reseeds from `config.yaml` if Redis empty.

`PIIDetector` — 3-pass (exclusion → named fields → standalone). 20+ patterns (email, phone, credit card, Chinese ID, passwords, API keys, connection strings). Strategies: sanitize / reject / hash.

## Debug Switches (16 total, hot-reloadable)

`config.yaml` `debug:` section — replaces the old `debug_mode` flag (kept only as a production safety-net that forces `AI_GATEWAY_ENV=production` → `debug_mode=False`, log level ≥ INFO).

- 4 dimensions: `frontend`, `entry`, `cache`, `bridge`
- 1 plugin master switch: `plugins_enabled`
- 11 per-plugin toggles: `per_plugin.{name}` (AND with `plugins_enabled`)

`TraceCollector.emit_debug(...)` gates by dimension. Admin endpoints: `POST /admin/plugins/{name}/debug`, `GET /admin/config/debug`, `PUT /admin/global-config`. Control panel: "Debug switches" card lives under Plugins page → Global config.

5xx `detail` always redacts (not gated by any debug flag). `@app.exception_handler(Exception)` gives a uniform envelope with `X-Request-ID`.

## Key Files

| File | When to open |
|---|---|
| `config.yaml` | Runtime params, add provider, toggle plugins (hot-reloadable). |
| `config.yaml.template` | Schema reference — check before adding a field. |
| `docker-compose.yml` | 6 services (gateway, control-panel, redis, qdrant, prometheus, grafana). |
| `aigateway-api/Dockerfile` | Layered install (apt → torch → requirements.txt → Qwen3 model → src). |
| `aigateway-api/requirements.txt` | All Python deps. Bump here, not in Dockerfile. |
| `Dockerfile.frontend` | Node 20 build → Nginx serve. |
| `.env.example` / `.env.docker` | Runtime env template / BuildKit switch. `.env` itself gitignored. |
| `docs/DB_SCHEMA.md` | Redis keys, Qdrant collections, PipelineContext. |
| `docs/ARCHITECTURE_DIAGRAM.md` | Full 总分总 / dual-entry diagram. |
| `dispatcher.py` | Request flow, classification, cache backfill. |
| `openai_compat.py` | SSE streaming, response assembly, request logging. |
| `admin_routes.py` | Admin endpoints (keys/quotas/plugins/logs/RAG/L3). |
| `main.py` | App factory, lifespan init, both PipelineEngine instances. |
| `pipeline.py` | PipelineEngine + classic plugins. |
| `caching.py` | Cache-key gen, L1/L2/L3, rerankers, backfill. |
| `security.py` | KeyStore, PIIDetector. |
| `litellm_bridge.py` | Multi-provider calls, fallback, cooldown, auto resolver. Cooldown reads `circuit_breaker:` section. |
| `generation_optimization/` | 6 gen plugins + 8 strategies. |
| `media/` | Media Optimization V2. |
| `control-panel/src/pages/` | 9 page components. |
| `control-panel/src/api/client.ts` | Fetch calls + `parseMetrics()` (client-side Prometheus text parse). |

## Development

### Backend (local)
```bash
# Editable installs — core first
cd aigateway-core && pip install -e .
cd ../aigateway-api && pip install -e .
cd ../aigateway-cli && pip install -e .

uvicorn src.aigateway_api.main:create_app --factory --host 0.0.0.0 --port 8000 --reload
aigateway chat            # CLI
```

### Frontend (local)
```bash
cd control-panel && npm install
npm run dev               # Vite :5173, proxies /aigateway/* → :8000
npm run build             # tsc -b && vite build
```
Prod uses nginx `/aigateway/*` → `http://gateway:8000/`.

### Docker
```bash
sudo DOCKER_BUILDKIT=1 docker compose up -d --build gateway         # backend
sudo DOCKER_BUILDKIT=1 docker compose up -d --build control-panel   # frontend
docker compose up -d      # start all 6
docker compose down
```
`.dockerignore` keeps context <10MB. Or `set -a && source .env.docker && set +a` to skip the BuildKit prefix. Services: gateway `:8000`, panel `:3000`, prometheus `:9090`, grafana `:3001` (admin/admin).

### Testing
```bash
python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py   # flaky, skip
python3 -m pytest tests/test_cache_key_v2.py -v
python3 -m pytest tests/ --cov=aigateway_core --cov=aigateway_api
```
25 files. No `conftest.py` / `pytest.ini`. Env uses `python3` (no `python` alias).

### Config precedence (high → low)
1. Real process env (docker-compose `environment:` / shell `export`)
2. `.env` file (`load_dotenv(override=False)`)
3. `config.yaml` literal values
4. `_DEFAULT_CONFIG` in code

- `AI_GATEWAY_*` env vars override YAML sections.
- `AI_GATEWAY_GENERATION_OPTIMIZATION_*` overrides that subsection.
- `hot_reload: true` enables Watchdog for live YAML updates.
- `AI_GATEWAY_ENV=production` forces safe defaults.

## Important Patterns

1. **App state via lifespan** — all shared components on `app.state`, read by `_get_app_state()`.
2. **sys.path shim** — `main.py` prepends `aigateway-core/src`; Dockerfile places packages differently.
3. **Prometheus lazy init** — metrics created on first `_ensure_initialized()` call. Duration histogram buckets: `[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0]`.
4. **Frontend parses Prom text** client-side via `parseMetrics()`. No structured metrics endpoint.
5. **Deps live in `requirements.txt`** — not the Dockerfile. Editable installs work because packages are plain `src/` dirs. torch installed separately as a pre-layer.
6. **Layered plugin registration** — `_register_builtin_plugins()` (classic) and `register_generation_optimization_plugins()` (gen-opt) both feed the same `PluginRegistry`.
7. **Embedding model cached** — `_l3_model_cache` module-level in `openai_compat.py` avoids reloading ~600MB Qwen3.
8. **Hot-reload loop** — admin PUT → file write → `atomic_swap` → `_notify_reload` → `main._on_config_reload` rebuilds both PipelineEngines.
9. **contextvar logging** — `ContextInjectProcessor` reads trace_id via `TraceCollector.current()` (fixed a per-class-dict race).
10. **Per-model `base_url` override** — providers like Agnes route text/image/video to different endpoints. Set `providers.<name>.model_grouper[].models[].base_url`; fallbacks always inherit provider-level URL.
11. **Single-worker** — `workers: 1` in Dockerfile CMD; the config field is deprecated.
12. **Streaming parity** — SSE path also decrements quota, backfills cache, uses real cost.

## Known States & Gotchas

- **Understanding pipeline runs only 2 plugins in engine** — 7 registered, `dispatcher._skip_names` filters out 5 (pii/cache/semantic/compress/media, all already run in the shared prefix); engine executes `rag_retriever + conv_compressor` only. Generation pipeline sets no skip → all 6 gen-opt plugins run.
- **model_router plugin is fully removed** — real routing lives in `LiteLLMBridge` auto resolver. `classify_request` only handles modality. Don't confuse with `gen_model_router` (different plugin, generation pipeline).
- **`PIIDetector` / `PromptCompressPlugin` double-instantiated** — one in registry (skipped for understanding) + one on `app.state` (actually runs). Inline-integration artifact, not a bug.
- **prompt_compress** — real LLMLingua-2 impl. `device: cpu|cuda`, `compression_ratio`, `target_token` in config.
- **rag_retriever / conv_compressor** — default-enabled with local fallback (Qdrant needed only for full retrieval).
- **TokenCompressorStrategy** — deterministic hash-vector placeholder; real CLIP/ViT segmentation is a TODO.
- **`GenerationPipeline` (`media/generation.py`) is orphaned** — 0 prod references. Gen path is the 6-plugin chain.
- **AIDirectorStrategy late-binds bridge** — registration runs before bridge exists; `main.py` injects `_litellm_bridge` post-init.
- **Dead frontend code** — `hooks/useAuth.ts`, `hooks/usePoll.ts` have 0 imports. Six API client fns (`createChatCompletion*`, `listModels`, `createEmbeddings`, `getQuota`, `getMetricsJson`) are reserved for Entry B.
- **Implicit frontend auth** — no login page or Auth provider. `ensureAuthHeaders()` pulls key from localStorage silently; unset key → blank pages (except Plugins/Overview which handle it).

## Workflow Rules

1. **Auto-commit after every code-changing task.** Conventional prefix (`feat:` / `fix:` / `refactor:` / `docs:` / `test:` / `chore:`). If `git add` / `git commit` hits a conflict, stop and ask — never force-resolve.
2. **Rebuild Docker when the change lives in the image.** Backend Python under `aigateway-*/`, Dockerfile edits, new deps, or baked-in `config.yaml` structural changes → `docker compose up -d --build {gateway|control-panel}` then `curl -sf localhost:8000/health` + `docker compose logs --tail=50 gateway | grep -i error`. Live YAML edits under `hot_reload: true`, frontend under `npm run dev`, and pure docs don't need a rebuild. Report the rebuild+verify result with the commit; surface failures, never skip silently.
3. **Keep this file current.** After any task that changes architecture, adds/removes a major component, alters config schema or commands, update the affected section here in the same task.
4. **Trim this file periodically. Cap ~300 lines.**
   - Before editing CLAUDE.md, run `wc -l CLAUDE.md`. If it's over ~300 (or the delta would push it over), first prune before adding.
   - Prune targets: (a) Known-States entries older than 30 days that describe already-merged work — collapse into one line or drop; (b) duplicate descriptions of the same subsystem across sections; (c) verbose Chinese where English is equivalent (prefer English — same info, ~25% fewer tokens); (d) commentary explaining historical PR reasoning — that belongs in git log, not here.
   - Prune signals: a section duplicates the ASCII overview; an entry starts with "PRn" or "已修复"; the tone is narrative ("我们把 X 改成 Y 因为..."). Keep only the current-state fact.
   - Never delete: current architecture, key-file map, config precedence, workflow rules, active gotchas that surprise new contributors.
5. **Careful merges, then push.** Check for functional conflicts (not just `<<<` markers) — two clean applies can still override each other's intent. Recommend a side with reasoning before asking. After a merge to `main` (and any needed rebuild+verify), push without waiting to be asked — that's the one exception to rule 1's no-push default.
6. **Token-efficient navigation.**
   - Prefer LSP for symbols (`goToDefinition` / `findReferences` / `workspaceSymbol` / `hover` / call hierarchy) over grep. Covers `.ts/.tsx/.js/.py/.go`. If LSP says "No LSP server available", set it up per `README.md` (pyright + typescript-language-server + `ENABLE_LSP_TOOL=1`), then continue.
   - Start from this file's Key Files table and Architecture diagram — don't `cat` the whole repo or read `repomix-output.md` in full.
   - `Grep` for patterns → `Read` the hit range with `offset`/`limit`. Don't Read whole large files.
   - Bug from a stack/trace → jump to `file:line` ±50 lines. Trace details at `/admin/trace/{id}`.
   - Fan-out searches (multi-file) → dispatch `Explore` / `general-purpose` subagent; main context only gets the conclusion.
   - Contract changes → cross-check `dispatcher.py` / `openai_compat.py` for API shape, `docs/DB_SCHEMA.md` for cache keys, `config.yaml.template` for config schema.
