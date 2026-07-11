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
aigateway-api/src/aigateway_api/    FastAPI app (protocol surface only)
  main.py               App factory, lifespan (init state, build both PipelineEngines)
  dispatcher.py         Thin adapter; real RequestDispatcher in core dispatch/dispatcher.py
  openai_compat.py      /v1/chat/completions handler + helpers (_apply_*/_record_request_log)
  admin_routes.py       API key CRUD, quotas, plugin config, logs, RAG, L3 cache mgmt
  streaming.py          SSE adapter (create_sse_response); real SSEGenerator in core route/streaming/
  routes.py             /health, /metrics
  auth_middleware.py    Bearer/x-api-key validation
  rate_limiter.py       IP rate limit
  trace_middleware.py   Generates/propagates trace_id -> request.state
  draft_routes.py, template_routes.py, code_rag_routes.py

aigateway-core/src/aigateway_core/  Shared library - runtime skeleton (prefix/dispatch/pipelines/route/shared)
  prefix/               Shared pre-routing layer
    pii/                PIIDetector (detector.py) + PIIDetectorPlugin (plugin.py)
    cache/              CacheManager L1->L2->L3, cache-key v2 (cache_keys/cache_manager/l3_semantic) + PromptCache/SemanticCache plugins
    media/              Media Optimization V2: plugin, mol, cache, pipelines/{image,video,audio,document}
    registration.py     _register_builtin_plugins (classic + rag/conv/media + gen-opt)
  dispatch/             RequestDispatcher, PipelineEngine, PipelineContext, classify_request
  pipelines/
    understanding/      rag/ (RAGRetriever), conversation/ (ConvCompressor), compression/ (PromptCompress LLMLingua-2), code_rag/
    generation/         director/intent/token/draft/cost/routing_signals/ (6 plugins + strategies) + _common/ (config/models/metrics/exceptions/api_key_groups) + registration.py
  route/                bridge/ (LiteLLMBridge + cooldown), streaming/ (SSE + cache_stream + metrics_wrapper), metrics/ (costing), model_resolution/ (ModelRouterStrategy auto resolver)
  shared/               config, tracing, trace_event, exceptions, plugin_registry, logger, metrics, debug_config, redis_client, qdrant_client, integration_configs, auth/key_store, auth/group_store

aigateway-cli/src/aigateway_cli/    __main__, chat, run, session
control-panel/src/                  App.tsx (routes), api/client.ts, pages/ (9), components/, hooks/
```

## Cache Key v2 (2026-07-06)

L2 prefix `aigateway:cache:v2:*`. v1 keys expire naturally, not purged.

```
key = SHA-256("v2" | pipeline_kind | model_family | temp_bucket | mt_bucket
              [ | u=user_id if scope=private ]
              [ | g=group_id if scope=group ] | normalized_prompt)
```

- **model_family**: strips date snapshot (`gpt-4o-2024-08-06` → `gpt-4o`); `auto` kept literal. New snapshots don't bust cache.
- **temp_bucket**: `exact_zero` (≤0.05) / `det` (≤0.3) / `bal` (≤0.9) / `cre` (>0.9).
- **mt_bucket**: rounded up to `le_256/512/1024/2048/4096/8192/16384`; `None/0` → `any`.
- **top_p**: ignored (nearly always 1.0 in practice).
- **cache_scope**: three tiers — `private` (user-isolated, PII-forced), `group` (shared among group members, default), `public` (globally shared). Decision: header `X-Cache-Scope` > PII-forced `private` > default `group` (see `dispatcher._resolve_cache_scope`).
- **normalized_prompt**: system + last 3 turns only (`dispatcher._extract_cacheable_context`), NFKC + whitespace collapse (`prefix.cache.cache_keys._normalize_prompt`).
- **metrics**: `gateway_cache_hits_total{tier}` / `gateway_cache_misses_total` counted at dispatcher (fixed a v1 blind spot).
- **tenant_id**: removed (unused — `group_id` replaces it for multi-tenant isolation).
- Tests: `tests/test_cache_key_v2.py`.

## Three-Tier Cache

| Tier | Store              | Latency | Size cap    | TTL     |
|------|--------------------|---------|-------------|---------|
| L1   | `cachetools.LRU`   | <1ms    | ≤100KB/entry, 1000 entries | in-process |
| L2   | Redis + LZ4        | few ms  | ≤500KB/entry | ~3600s |
| L3   | Qdrant (Qwen3-Embedding-0.6B 1024-dim, cosine ≥0.95) | ~50ms | — | ~86400s |

Backfill: L2 hit → L1; L3 hit → L1 only (approximate); MISS → L1+L2 + async L3 (if token_count ≥100). L3 has retrieve→rerank two stage. `qdrant_client.search/retrieve` treats 404 as miss (collection lazy-created on first `set_l3`).

## Security & Quotas

`KeyStore` — Redis hash per key. Per-key: daily tokens (default 1M), monthly cost ($50), RPM (60), TPM (100K). Pub/Sub sync across instances. Auto-reseeds from `config.yaml` if Redis empty. Quota enforcement uses an atomic Lua script (`check_quota` → `EVALSHA`/`EVAL`) that checks all 4 dims for key+group in one round-trip and bumps counters immediately (TOCTOU-safe). The pre-flight estimate (token count ÷4, cost=0) is reserved; `increment_usage` reconciles the delta (actual − reserved) post-LLM via `_reserved_tokens`/`_reserved_cost`. FakeRedis (tests) lacks `eval` → falls back to non-atomic `_check_quota_legacy`.

`GroupStore` — Redis hash per group (`aigateway:group:{group_id}`). Group-level quotas (daily tokens, monthly cost, RPM, TPM) shared pool for all member keys. Per-key personal quotas are sub-limits within the group. Quota check: group first, then personal. Error codes prefixed `Group ` for group-level rejection. Group members stored as Redis Set (`aigateway:group:{gid}:members`). Group events broadcast via `aigateway:groups:sync` Pub/Sub channel.

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
| `docs/RUNTIME_MAP.md` | Legacy path → 总分总 runtime layer (prefix/dispatch/pipelines/route/shared). |
| `docs/ARCHITECTURE_DIAGRAM.md` | Full 总分总 / dual-entry diagram. |
| `dispatcher.py` (api) | Thin adapter; real flow in `aigateway_core/dispatch/dispatcher.py`. |
| `openai_compat.py` | SSE streaming, response assembly, request logging. |
| `admin_routes.py` | Admin endpoints (keys/quotas/plugins/logs/RAG/L3). |
| `main.py` | App factory, lifespan init, both PipelineEngine instances. |
| `dispatch/dispatcher.py` | Request flow, classification, cache backfill (core). |
| `prefix/cache/` | Cache-key gen, L1/L2/L3, rerankers, backfill. |
| `prefix/pii/` + `shared/auth/` | PIIDetector, KeyStore. |
| `route/bridge/litellm_bridge.py` | Multi-provider calls, fallback, cooldown, auto resolver. Cooldown reads `circuit_breaker:` section. |
| `pipelines/generation/` | 6 gen plugins + strategies (+ `_common/`, `registration.py`). |
| `prefix/media/` | Media Optimization V2. |
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
3. **Prometheus lazy init** — metrics created on first `_ensure_initialized()` call. Duration histogram buckets: `[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0]`. New: `gateway_cost_by_group{group_id}` counter tracks per-group cost.
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

- **Code RAG is now a separate subsystem** — Control Panel Knowledge page has a Code tab with async imports (folder/server_path/git/zip), dedicated `/admin/rag/code/*` routes, per-model `rag_code_*` Qdrant collections, and per-repo CodeGraph SQLite files under `/data/code_graphs`. Graph build uses the official `@colbymchenry/codegraph` CLI (`codegraph init` / `codegraph index`), not a Python `codegraph` API. Graph query has **strict/tolerant split**: import path calls `lookup_symbol_metadata_strict` (any SQLite/schema failure fails the whole import), retrieval calls the tolerant wrapper (never breaks the text-RAG chain). Retrieval also honors `code_rag_graph_hops` — a BFS over `edges.kind='calls'` fetches related-symbol chunks from the same collection.
- **Understanding pipeline runs only 2 plugins in engine** — 7 registered, `dispatcher._skip_names` filters out 5 (pii/cache/semantic/compress/media, all already run in the shared prefix); engine executes `rag_retriever + conv_compressor` only. Generation pipeline sets no skip → all 6 gen-opt plugins run.
- **model_router plugin is fully removed** — real routing lives in `LiteLLMBridge` auto resolver. `classify_request` only handles modality. Don't confuse with `gen_model_router` (different plugin, generation pipeline).
- **`PIIDetector` / `PromptCompressPlugin` double-instantiated** — one in registry (skipped for understanding) + one on `app.state` (actually runs). Inline-integration artifact, not a bug.
- **prompt_compress** — real LLMLingua-2 impl. `device: cpu|cuda`, `compression_ratio`, `target_token` in config.
- **rag_retriever / conv_compressor** — default-enabled with local fallback (Qdrant needed only for full retrieval).
- **TokenCompressorStrategy** — deterministic hash-vector placeholder; real CLIP/ViT segmentation is a TODO.
- **`GenerationPipeline` (`prefix/media/generation.py`) is orphaned** — 0 prod references. Gen path is the 6-plugin chain.
- **AIDirectorStrategy late-binds bridge** — registration runs before bridge exists; `main.py` injects `_litellm_bridge` post-init.
- **Dead frontend code** — `hooks/useAuth.ts`, `hooks/usePoll.ts` have 0 imports. Six API client fns (`createChatCompletion*`, `listModels`, `createEmbeddings`, `getQuota`, `getMetricsJson`) are reserved for Entry B.
- **Implicit frontend auth** — no login page or Auth provider. `ensureAuthHeaders()` pulls key from localStorage silently; unset key → blank pages (except Plugins/Overview which handle it).
- **Config writes must be atomic** — admin endpoints (`update_plugins_config`, `set_plugin_debug`, `update_global_config`) write `config.yaml` via `_atomic_write_yaml` (tempfile + `os.replace`). The Watchdog `load()` reads the file *without* `fcntl.flock`, so the old `open(w)+yaml.dump` (truncate-then-write) let it read a half-written file → `DebugConfigWatcher`/`PluginRegistry` got stale state. Never revert to non-atomic writes here.
- **`plugins_enabled` flat ↔ nested** — `DebugConfig.from_dict` prefers nested `debug.plugins.enabled` over flat `debug.plugins_enabled`. `update_global_config` normalizes both forms before persisting so the control panel's flat `toggleDebugDimension('plugins_enabled')` takes effect. New debug writers must set both (or go through `update_global_config`).
- **Generation plugins toggled via `generation_optimization.<sub>.enabled`** — the 6 gen-opt plugins aren't in `config.yaml`'s `plugins:` list; `update_plugins_config` maps them via `_GENERATION_PLUGIN_CONFIG_PATH` and forces `generation_optimization.enabled=true`.
- **Dockerfile must ship `build-essential`** — torch 2.13 + CUDA JIT-compiles kernels via triton, which needs a C compiler. Without `gcc`, the first embedding forward pass (L3 semantic cache backfill / RAG retrieval) **synchronously blocks ~22s** inside the single uvicorn worker → `/health` times out → Control Panel shows no data. triton doesn't cache "compile failed", so every fresh request re-blocks. `build-essential` in the apt layer is the fix (added 2026-07-09). Don't remove it to shrink the image.

## Workflow Rules

0. **Review before commit.** After fixing bugs or adding features, run `window-code-review` skill on the diff first. Address confirmed findings. **Never auto-commit.** All code changes remain in the working tree until the user reviews and explicitly approves.
1. **Rebuild Docker when the change lives in the image.** Backend Python under `aigateway-*/`, Dockerfile edits, new deps, or baked-in `config.yaml` structural changes → `docker compose up -d --build {gateway|control-panel}` then `curl -sf localhost:8000/health` + `docker compose logs --tail=50 gateway | grep -i error`. Live YAML edits under `hot_reload: true`, frontend under `npm run dev`, and pure docs don't need a rebuild. Report the rebuild+verify result with the commit; surface failures, never skip silently.
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
