# Hardcoded Values and Parameters in AI Gateway Codebase

## Report Overview

This document identifies hardcoded (non-configurable) values embedded directly in the codebase that should instead be configurable via `config.yaml`, environment variables, or runtime configuration. These include magic strings, numeric constants, URLs, file paths, timeouts, thresholds, and other parameters that lack proper abstraction from configuration.

**Critical**: Some hardcoding creates maintenance burdens, deployment inflexibility, and potential security concerns where sensitive values are embedded directly in source.

---

## 1. Model Names and Provider Endpoints

| File | Line | Value | Issue | Recommendation |
|------|------|-------|-------|---------------|
| `aigateway-api/src/aigateway_api/admin_routes.py` | Multiple | Multiple provider base URLs (`https://api.openai.com/v1`, `https://api.anthropic.com/v1`, etc.) | Duplicate definitions; should be unified in config | Move all provider endpoint templates to `config.yaml.providers` |
| `aigateway-core/src/aigateway_core/shared/integration_configs.py` | L29 | `model_name = "microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank"` | Prompt compression model name is hardcoded | Use configurable default or env var override |
| `aigateway-core/src/aigateway_core/shared/integration_configs.py` | L45 | `model_name = "openai/clip-vit-large-patch14"` | CLIP vision model name is hardcoded | Allow config override for different CLIP variants |
| `aigateway-core/src/aigateway_core/shared/integration_configs.py` | L63 | `summary_model = "agnes-2.0-flash"` in ConvCompressorConfig | Summary generation model is hardcoded | Make configurable via `config.plugins.conv_compressor.config.summary_model` |
| `aigateway-core/src/aigateway_core/shared/integration_configs.py` | L154 | `rerank_model = "cross-encoder/ms-marco-MiniLM-L-6-v2"` | Reranker model name is hardcoded | Allow reranker model selection via config |
| `aigateway-core/src/aigateway_core/shared/integration_configs.py` | L163 | `embedding_model = "Qwen/Qwen3-Embedding-0.6B"` | Embedding model name used throughout RAG | While consistent with L3 cache, allow override per RAG instance |

---

## 2. File System Paths and Directories

| File | Line | Value | Issue | Recommendation |
|------|------|-------|-------|---------------|
| `aigateway-core/src/aigateway_core/pipelines/generation/_common/config.py` | L114 | `store_dir = "/app/data/drafts"` | Draft storage path hardcoded | Should use config-based draft directory |
| `aigateway-core/src/aigateway_core/pipelines/generation/draft/draft_cleaner.py` | L39 | `self._store_dir = store_dir or "/data/drafts"` | Hardcoded path in cleaner logic | Use config value consistently |
| `aigateway-core/src/aigateway_core/pipelines/generation/draft/draft_generator.py` | L108 | `self._store_dir = store_dir or getattr(config, "store_dir", "/app/data/drafts")` | Fallback to hardcoded path | Remove fallback, require explicit config |
| `aigateway-core/src/aigateway_core/shared/integration_configs.py` | L174 | `code_graph_db_dir = "/data/code_graphs"` | Code RAG database directory hardcoded | Should be configurable via top-level setting |
| `aigateway-core/src/aigateway_core/pipelines/understanding/rag/rag_retriever_plugin.py` | L682 | `graph_dir = getattr(self._config, "code_graph_db_dir", "/data/code_graphs")` | Duplicate hardcoded path in RAG plugin | Consolidate with config central value |
| `aigateway-core/src/aigateway_core/shared/auth/sqlite_store.py` | L324 | `path = "data/auth.db"` | Relative auth DB path is hardcoded | Use absolute path via `AI_GATEWAY_AUTH_DB_PATH` env var only |
| `aigateway-core/src/aigateway_core/shared/integration_configs.py` | L78-L80 | `models_path = "/comfyui/models"`, `output_path = "/comfyui/output"`, `workflow_path = "/comfyui/workflows"` | ComfyUI filesystem paths hardcoded | Allow these to be mounted as volumes without code changes |

---

## 3. Network Hosts, Ports, and URLs

| File | Line | Value | Issue | Recommendation |
|------|------|-------|-------|---------------|
| `aigateway-api/src/aigateway_api/main.py` | L919 | `cors_origins = ["http://localhost:3000", "http://localhost:5173"]` | CORS origins hardcoded as fallback; should fully respect config | Config already supports CORS but this fallback bypasses it |
| `aigateway-api/src/aigateway_api/local_generation.py` | L62, L67, L72 | `server_url = "http://localhost:8188"`, multiple references to localhost:8188 | ComfyUI server URL hardcoded | Use config value from `integration_configs` or `generation_optimization.comfyui.server_url` |
| `aigateway-core/src/aigateway_core/shared/integration_configs.py` | L61-L62 | `server_url = "http://localhost:8188"`, `public_url = "http://localhost:8188"` | Localhost assumptions for ComfyUI | Should accept external hostnames for remote deployment |
| `aigateway-core/src/aigateway_core/shared/integration_configs.py` | L160 | `api_base = "http://localhost:8000/v1"` in ConvCompressorConfig | API base for local routing hardcoded | Use dynamic gateway URL from env or config |
| `aigateway-core/src/aigateway_core/shared/qdrant_client.py` | L52 | `self.url: str = "http://localhost:6333"` | Qdrant client default localhost | Should read from `AI_GATEWAY_QDRANT_URL` only, no hardcoded fallback without explicit config |

---

## 4. Numeric Constants / Magic Numbers

| File | Line | Value | Type | Issue | Recommendation |
|------|------|-------|------|-------|---------------|
| `aigateway-core/src/aigateway_core/route/metrics/costing.py` | L10-15 | Various pricing values (e.g., `gpt-4o: 0.000005`) | Pricing | Hardcoded pricing tables must stay synchronized with `litellm_bridge.py` pricing | Centralize pricing data in config or use LitellM's built-in pricing |
| `aigateway-core/src/aigateway_core/route/bridge/litellm_bridge.py` | L105-110 | Same pricing values duplicated | Pricing | Redundant copy causes desync risk | Remove from bridge; reference single source of truth |
| `aigateway-core/src/aigateway_core/prefix/cache/cache_keys.py` | L14 | `_MAX_TOKENS_BUCKETS = [256, 512, 1024, 2048, 4096, 8192, 16384]` | Token buckets | Reasonable defaults but could be adjustable | Add optional config override |
| `aigateway-core/src/aigateway_core/prefix/cache/cache_manager.py` | L55 | `L1_MAX_VALUE_BYTES = 102400` | Cache entry limit | Fixed threshold for L1 caching | Could be tunable based on memory constraints |
| `aigateway-core/src/aigateway_core/prefix/cache/cache_manager.py` | L57 | `L3_DEFAULT_TTL = 86400` | 24h TTL | Hardcoded L3 cache TTL | Should be configurable per collection type |

---

## 5. Media and Generation-Specific Hardcodes

| File | Line | Value | Issue | Recommendation |
|------|------|-------|-------|---------------|
| `aigateway-core/src/aigateway_core/shared/integration_configs.py` | L68 | `workflow_version = "image-v1"` | ComfyUI workflow version hardcoded | Allow custom workflow templates via config |
| `aigateway-core/src/aigateway_core/shared/integration_configs.py` | L69 | `checkpoint_name = "sd_xl_base_1.0.safetensors"` | Default checkpoint fixed | Allow checkpoint selection via UI/API |
| `aigateway-core/src/aigateway_core/shared/integration_configs.py` | L82 | `allowed_upscale_models = ["RealESRGAN_x4plus.pth"]` | Allowed upscale models list fixed | Dynamic registration would be better |

---

## 6. String Templates and Error Messages

| File | Line | Value | Issue | Recommendation |
|------|------|-------|-------|---------------|
| `aigateway-core/src/aigateway_core/prefix/cache/l2_search.py` | L51-52 | `L2_INDEX_NAME = "aigateway:l2:idx:v3"`, `L2_HASH_PREFIX = "aigateway:cache:v3search:"` | Redis key prefixes | These should be configurable to avoid collisions in shared Redis instances |
| `aigateway-core/src/aigateway_core/prefix/media/cache.py` | L38 | `KEY_PREFIX = "aigateway:media"` | Media cache prefix | Configurable prefix for multi-tenancy |
| `aigateway-core/src/aigateway_core/pipelines/generation/token/prompt_template_manager.py` | L63-64 | `KEY_PREFIX = "aigateway:prompt_template"`, `INDEX_PREFIX = "aigateway:prompt_template_index"` | Prompt template keys | Configurable prefix needed for multi-tenant setups |

---

## 7. Authentication-Related Hardcoded Values

| File | Line | Value | Issue | Recommendation |
|------|------|-------|-------|---------------|
| `aigateway-api/src/aigateway_api/browser_auth.py` | L121 | `'admin'` in INSERT statement | Default admin username is fixed well-known value; while functionality is correct, security best practices suggest configurable default usernames | Consider reading from `ADMIN_USERNAME` env var if set, otherwise keep 'admin' as documented fallback |

---

## Summary by Severity

### Critical (Security & Reliability)
1. **Hardcoded DB paths** - May cause container/pod failures when data volume mounts differ
2. **Localhost assumptions** - Break deployments where services aren't on localhost (K8s, cloud, distributed setups)
3. **CORS fallback bypasses config** - Inconsistent behavior between dev and prod configurations

### High (Maintainability & Flexibility)
1. **Pricing table duplication** - Two copies risk desync → incorrect cost tracking
2. **Redis key prefixes not configurable** - Shared Redis instance collision risk
3. **ComfyUI hard-coded URLs** - Prevents remote ComfyUI integration without code changes

### Medium (Usability)
1. **Model names fixed in defaults** - Limits experimentation with different ML models
2. **Media pipeline constants** - Fine-tuning requires code changes instead of config edits
3. **Magic numbers in caching** - Truncation lengths, bucket sizes could be tuned per workload

### Low (Documentation & Hygiene)
1. **Test file constants** - Acceptable in tests
2. **Well-known default admin username** - Documented behavior, security risk mitigated by requiring initial password change on first login

---

*Generated: Automated scan of AI Gateway codebase on 2026-07-29*
