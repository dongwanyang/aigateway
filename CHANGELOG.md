# Changelog

## [0.1.1.0] - 2026-07-25

### Added
- **Chat-window video generation**: Sending a video prompt (e.g. "生成一段日落海面的视频") in the Control Panel `/chat` page now produces a video. The draft-confirm flow calls the Agnes `/videos` endpoint asynchronously, returns a `video_id`, and the chat message polls `GET /v1/videos/{id}` until the video is ready, then renders it inline.
- **Video draft-confirm path**: `confirm_draft` dispatches on `media_type` — video drafts call `_confirm_video_draft` → `LiteLLMBridge._do_video_generation`, persist the `video_id` alongside the draft, and return `{video_id, status, media_type}` to the frontend.
- **Video intent heuristics**: The intent classifier now recognizes Chinese and English video-generation phrasing so requests route to `generation:video` even when the LLM judge is unavailable (timeout fallback).
- **Frontend video playback**: `MediaVideo` accepts explicit `videoId`/`videoUrl` props, polls the video-status endpoint, and treats `status='completed'` as terminal.

### Fixed
- **Video drafts silently routed to image upscale**: `DraftGeneratorPlugin._build_generation_request` always set `media_type='image'`, so `generation:video` drafts hit the image-upscale branch in confirm. `media_type` is now derived from `ctx.pipeline_kind` (`generation:video`→`video`, else `image`).
- **Completed videos never rendered**: Agnes `/v1/videos/{id}` returns the finished URL in `metadata.url`, not the top-level `url`/`video.url` fields the extractor checked. `extractVideoUrl` now tries all three locations; without this, completed tasks resolved to `null` and `MediaVideo` stayed stuck polling forever.
- **Broken `<video>` in unconfirmed draft preview**: `DraftCard` rendered a `<video>` tag for a keyframe image. It now uses the image lightbox for the keyframe preview.
- **Silent `confirm_draft` 400s**: The `confirm_draft` exception handler re-raised as 400 without logging, leaving no traceback. It now logs `logger.error(..., exc_info=True)`.

### Changed
- **Provider model config migrated to `capabilities`**: All providers in `config.yaml` now use the multi-select `capabilities: [text,image,video]` form (replacing `modality` strings). A backward-compat shim (`_normalize_model_capabilities`) lets legacy `modality` configs still route correctly.

## [0.1.0.1] - 2026-07-24

### Fixed
- **Code RAG import stuck forever on large repos**: Replaced per-symbol `codegraph` CLI subprocess spawning (~10k spawns for a 5k-symbol repo) with 2 SQL queries against the codegraph SQLite db (114ms). Callers/callees are now resolved via in-process cache invalidated by file-hash snapshot.
- **Orphaned import tasks after gateway restart**: Startup sweep marks non-terminal tasks as `failed` so the frontend no longer shows "importing" indefinitely.
- **No progress during splitting phase**: Import progress is now written to SQLite every 200 symbols, giving users real-time visibility into how long imports take.
- **Task state lost on restart**: Task state moved from Redis keys to SQLite `code_rag_tasks` table with history retention, pagination, and conditional cancel.

### Added
- **Corrupt-db detection**: Import fails loudly on a corrupted codegraph db instead of silently producing empty callers/callees.
- **Thread-safe edges cache**: LRU cache guarded by `threading.Lock` prevents `RuntimeError` from concurrent access by admin routes and RAG retrieval paths.

### Changed
- Code RAG task queries paginated (`?limit=50&offset=0`) to prevent unbounded scans on large task histories.

---

## [0.1.0.0] - 2026-07-21

### Added
- **Intent-driven routing**: Async LLM-based intent pre-judge classifies requests as understanding / generation:image / generation:video before pipeline execution. Model selection now uses multi-select `capabilities` pools instead of modality strings, selecting the cheapest matching model per capability intersection.
- **Image and video generation paths**: New `_do_image_generation` (OpenAI Images API) and `_do_video_generation` (async /videos endpoint) in the LiteLLM bridge, with non-streaming and SSE streaming support, error handling, and `extra_headers` propagation.
- **Async video task tracking**: `TaskTracker` persists video generation tasks in Redis (or in-memory fallback) with SCAN-based active listing, TTL, and status lifecycle management.
- **Chat window MVP (Entry B)**: Control Panel `/chat` page with SSE streaming, multi-session list, draft cards, image/video rendering, typing indicators, routing badges, and message bubble polish.
- **SQLite auth store**: Drop-in replacement for Redis-backed KeyStore/GroupStore using WAL-mode SQLite with atomic conditional UPDATE for quota enforcement.
- **L2 BM25 cache with Friso Chinese tokenization**: L2 cache rebuilt on Redis Stack RediSearch full-text search (BM25) instead of exact SHA-256 hash + LZ4. Uses RediSearch's built-in Friso library for CJK segmentation (`LANGUAGE_FIELD doc_lang` + `language=chinese` on index, `doc_lang=chinese` on each Hash, `.language("chinese")` on query) — no jieba dependency. `response_json` is stored on the Hash but excluded from the index schema to avoid diluting BM25 IDF scores. Catches near-duplicate Chinese prompts without embedding compute; fully paraphrased prompts still fall through to L3 Qdrant semantic cache.
- **Trace event consolidation**: Control Panel traces page merges pipeline trace events into one row per stage.

### Changed
- Video route returns 503 with `bridge_unavailable` code when bridge is None; error messages sanitized behind debug gate.
- Draft preview placeholders no longer include raw user prompts to prevent PII/secret leakage through generated image bytes.
- TaskTracker `list_active` uses Redis SCAN instead of KEYS to avoid blocking the single uvicorn worker.
- Code RAG supports atomic task cancellation and auto-deletes completed tasks; frontend reflects task status correctly.
- Chat window resume logic: session-switch resume effect now depends on `[activeId]` only and reads `sessions`/`send` via refs, preventing the resume effect from re-firing on every `sessions` change and clobbering the empty assistant placeholder that `send` just appended (which caused draft responses to never render).

### Fixed
- TOCTOU race in SQLite `check_quota`: re-reads row inside transaction and applies conditional UPDATE atomically.
- Draft confirm/reject endpoints now enforce ownership authorization against authenticated admin principal.
- SSRF prevention applied to image/video generation URL validation.
- Cost ledger payloads separated by debug gate; exceptions sanitized in trace output.
- Plugin trace dual-write restored after prior regression; plugin metadata injected into trace events.
- Logs page records real model name and image/video draft requests instead of placeholder values.
- Seven e2e tests fixed after auth store migration and pipeline changes.
- SQLite auth DB switched to project-root `data/` directory with Docker bind-mount support.

### Tests
- Added 30 new unit tests covering stream/non-stream image and video intents, extra_headers propagation, `ModelSelector.get_health`, video processing states, and concurrent quota race conditions.
- Added 63 new unit tests: `test_l2_search.py` (L2 BM25 module — Friso index config, store/search, escape helpers, degradation paths, boundary score), `test_task_tracker.py` (TaskTracker register/get/update/list/delete in memory + Redis-mock modes, TTL preservation), `test_video_routes.py` (video polling endpoint — bridge unavailable, success, error masking, debug-gate detail exposure).

### Security
- `authenticate_admin` middleware now requires explicit `is_admin=True` flag — closes auth bypass where any valid API key could access admin endpoints.
- `create_api_key` no longer returns the raw API key in the response body (shown only once at creation time).
- `reject_draft` endpoint now fails-closed when draft ownership metadata is missing (matching `confirm_draft` behavior).
- API key hashing uses full SHA-256 (64-char hexdigest) instead of truncated 16-char prefix, reducing birthday-bound collision risk.
- Rate limiter bucketing uses structured ID patterns (digits/hex/UUID/key-prefix/base64url) instead of bare length check, preventing long static endpoint names from being misclassified as IDs and collapsing distinct endpoints into shared buckets.
- SSRF guard in draft image fetcher now disables httpx auto-redirects and validates redirect targets, preventing DNS rebinding and redirect-based bypass to cloud metadata endpoints. IPv4-mapped IPv6 addresses (`::ffff:x.x.x.x`) are now checked.

### Reliability
- SQLite auth store uses per-thread connections via `threading.local()` for safe `asyncio.to_thread()` usage on the validate hot path; quota operations (check_quota/increment_usage) deliberately stay on the event loop with a single shared connection for TOCTOU-safe atomic conditional UPDATEs.
- Added performance indexes for `key_prefix`, `user_id+status`, `group_id`, `quota_records`, and `group_members` columns.
- Fixed `migrate_groups()` call in main.py lifespan — was missing required `group_store` argument, which would crash on startup.
