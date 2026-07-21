# Changelog

## [0.1.0.0] - 2026-07-21

### Added
- **Intent-driven routing**: Async LLM-based intent pre-judge classifies requests as understanding / generation:image / generation:video before pipeline execution. Model selection now uses multi-select `capabilities` pools instead of modality strings, selecting the cheapest matching model per capability intersection.
- **Image and video generation paths**: New `_do_image_generation` (OpenAI Images API) and `_do_video_generation` (async /videos endpoint) in the LiteLLM bridge, with non-streaming and SSE streaming support, error handling, and `extra_headers` propagation.
- **Async video task tracking**: `TaskTracker` persists video generation tasks in Redis (or in-memory fallback) with SCAN-based active listing, TTL, and status lifecycle management.
- **Chat window MVP (Entry B)**: Control Panel `/chat` page with SSE streaming, multi-session list, draft cards, image/video rendering, typing indicators, routing badges, and message bubble polish.
- **SQLite auth store**: Drop-in replacement for Redis-backed KeyStore/GroupStore using WAL-mode SQLite with atomic conditional UPDATE for quota enforcement.
- **Trace event consolidation**: Control Panel traces page merges pipeline trace events into one row per stage.

### Changed
- Video route returns 503 with `bridge_unavailable` code when bridge is None; error messages sanitized behind debug gate.
- Draft preview placeholders no longer include raw user prompts to prevent PII/secret leakage through generated image bytes.
- TaskTracker `list_active` uses Redis SCAN instead of KEYS to avoid blocking the single uvicorn worker.
- Code RAG supports atomic task cancellation and auto-deletes completed tasks; frontend reflects task status correctly.

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
