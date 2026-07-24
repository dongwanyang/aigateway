# TODOS

## Completed

### Intent-driven routing
- Async LLM intent pre-judge for understanding / generation:image / generation:video — **Completed:** 0.1.0.0 (2026-07-21)
- Capabilities-based model selection replacing modality strings — **Completed:** 0.1.0.0 (2026-07-21)
- Image and video generation paths in LiteLLM bridge — **Completed:** 0.1.0.0 (2026-07-21)
- TaskTracker async video task persistence layer — **Completed:** 0.1.0.0 (2026-07-21)

### Chat window MVP
- Control Panel /chat page with SSE streaming — **Completed:** 0.1.0.0 (2026-07-21)
- Multi-session list, draft cards, image/video rendering, routing badges — **Completed:** 0.1.0.0 (2026-07-21)

### Auth & quotas
- SQLite auth store replacing Redis KeyStore/GroupStore — **Completed:** 0.1.0.0 (2026-07-21)
- Atomic TOCTOU-safe quota check in SQLite — **Completed:** 0.1.0.0 (2026-07-21)
- Draft ownership authorization on confirm/reject — **Completed:** 0.1.0.0 (2026-07-21)
- SSRF prevention for generated media URLs — **Completed:** 0.1.0.0 (2026-07-21)

### Observability
- Trace event consolidation in control panel — **Completed:** 0.1.0.0 (2026-07-21)
- Real model + image/video draft requests in Logs page — **Completed:** 0.1.0.0 (2026-07-21)

### Code RAG
- Atomic cancel and auto-delete completed tasks — **Completed:** 0.1.0.0 (2026-07-21)

### Code RAG v2 (0.1.0.1)
- SQLite task state replacing Redis keys with history retention and pagination — **Completed:** 0.1.0.1 (2026-07-24)
- Startup orphan task sweep marking non-terminal tasks as failed — **Completed:** 0.1.0.1 (2026-07-24)
- Split-stage progress callback written every 200 symbols — **Completed:** 0.1.0.1 (2026-07-24)
- DB-direct callers/callees via 2 SQL queries replacing per-symbol CLI spawns — **Completed:** 0.1.0.1 (2026-07-24)
- Corrupt-db detection failing loudly on corrupted codegraph db — **Completed:** 0.1.0.1 (2026-07-24)
- Thread-safe edges cache with threading.Lock — **Completed:** 0.1.0.1 (2026-07-24)
- Lazy cached callers/callees with file-hash invalidation — **Completed:** 0.1.0.1 (2026-07-24)
- Build symbol chunks using read_call_edges + progress callback — **Completed:** 0.1.0.1 (2026-07-24)
- TaskTracker async video task persistence layer — **Completed:** 0.1.0.0 (2026-07-21)
