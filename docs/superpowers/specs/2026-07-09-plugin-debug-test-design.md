---
description: Integration test for plugin enable/disable + per-plugin debug + 5 global debug dimensions
created: 2026-07-09
status: approved
---

# Plugin & Debug Switch Integration Test Design

## Goal

Verify that every plugin's enable/disable toggle and every debug switch (5 global dimensions + per-plugin) actually works end-to-end through the admin API → config hot-reload → runtime behavior pipeline.

## Test Script

`tests/test_plugin_debug_integration.py`

Runs against a live FastAPI app via `TestClient`. Expects Redis and Qdrant to be available (or gracefully degrade for plugins that don't strictly need them).

## Plugin Inventory (13 total)

| # | Name | Pipeline | Depends On | Global Debug Dimension |
|---|---|---|---|---|
| 1 | pii_detector | understanding | — | entry |
| 2 | prompt_cache | understanding | — | cache |
| 3 | semantic_cache | understanding | prompt_cache | cache |
| 4 | prompt_compress | understanding | semantic_cache | entry |
| 5 | rag_retriever | understanding | semantic_cache | plugins_enabled |
| 6 | conv_compressor | understanding | — | plugins_enabled |
| 7 | ai_director | generation | — | plugins_enabled |
| 8 | intent_evaluator | generation | ai_director | plugins_enabled |
| 9 | token_compressor | generation | intent_evaluator | plugins_enabled |
| 10 | draft_generator | generation | token_compressor | plugins_enabled |
| 11 | gen_model_router | generation | draft_generator | plugins_enabled |
| 12 | cost_tracker | generation | gen_model_router | plugins_enabled |
| 13 | media_optimizer | understanding | — | entry |

Note: `prompt_compress` has no per_plugin debug (returns `null` in admin API). Its debug is covered by the `entry` global dimension.

## Test Phases

### Phase 0: Setup & Teardown

- Backup `config.yaml` to `config.yaml.test-bak`
- Ensure `hot_reload: true` and `debug_mode: true` in config
- Reset all plugin enabled states and debug switches to known defaults
- After all tests: restore `config.yaml` from backup, reset debug to all-off

### Phase 1: Individual Plugin + Per-Plugin Debug Tests

For **each** of the 13 plugins (order as listed above):

1. **Enable plugin**: `PUT /admin/plugins-config` → `{name, enabled: true}`
2. **Enable per-plugin debug**: `POST /admin/plugins/{name}/debug` → `{enabled: true}` (skip if plugin has no per_plugin debug, i.e. `prompt_compress`)
3. **Enable the corresponding global debug dimension** (see table above)
4. **Verify state**: `GET /admin/config/debug` → confirm enabled flags match
5. **Trigger request**: `POST /v1/chat/completions` with a payload designed to exercise this plugin
6. **Verify debug event**: Check `TraceCollector` for kind=`debug` event with matching stage/dimension
7. **Verify plugin behavior**: Plugin-specific assertion (see Phase 1.1 details below)
8. **Cleanup**: Disable plugin debug, disable global dimension, disable plugin

#### Phase 1.1: Plugin-Specific Verification

| Plugin | Trigger Payload | Behavior to Verify |
|---|---|---|
| pii_detector | Message containing email/phone pattern | Response content is sanitized (PII replaced); debug event contains sanitization details |
| prompt_cache | Short prompt, send twice | Second hit L1 cache; debug event shows cache hit/miss |
| semantic_cache | Long prompt (≥100 tokens) | L3 semantic retrieval attempted; debug event shows embedding/search |
| prompt_compress | Very long prompt (>500 chars) | Prompt shortened; debug event contains compression ratio |
| rag_retriever | Query-like message | RAG retrieval attempted; debug event shows top_k results or graceful degradation |
| conv_compressor | Multi-turn conversation | Conversation compressed; debug event shows token reduction |
| ai_director | Generation prompt | AI director processes request; debug event shows intent classification |
| intent_evaluator | Generation prompt with model param | Intent evaluated; debug event shows routing decision |
| token_compressor | Image-containing message | Feature extraction attempted; debug event shows token compression |
| draft_generator | Generation prompt | Draft workflow triggered; debug event shows draft result |
| gen_model_router | Generation prompt | Model routing signal emitted; debug event shows model selection |
| cost_tracker | Successful completion | Cost recorded; debug event shows token/cost metrics |
| media_optimizer | Image URL in message | Media optimization attempted; debug event shows resize/format |

### Phase 2: Global Debug Dimension Tests

For **each** of the 5 global dimensions (order: frontend → entry → cache → bridge → plugins_enabled):

1. **Enable the global dimension** via `PUT /admin/global-config`
2. **Trigger request**: `POST /v1/chat/completions` with appropriate payload
3. **Verify debug event**: `TraceCollector` contains at least one kind=`debug` event matching this dimension's stage
4. **Cleanup**: Disable the global dimension

#### Phase 2.1: Dimension Coverage Mapping

| Dimension | Expected Event Stages |
|---|---|
| frontend | ASGI middleware, trace_middleware |
| entry | auth, dispatcher, prompt_compress |
| cache | prompt_cache, semantic_cache (L1/L2/L3) |
| bridge | litellm_bridge (model call exit) |
| plugins_enabled | Any plugin with per_plugin=true AND plugins_enabled=true |

### Phase 3: Incremental Plugin + Debug Accumulation

Start from all-off. For iteration `n = 2, 3, ..., 13`:

1. Enable plugins 1..n (PUT plugins-config for each)
2. Enable per_plugin debug for each of plugins 1..n (skip prompt_compress)
3. **Enable all 5 global debug dimensions** (always on during cumulative phase)
4. Trigger request
5. Verify: `TraceCollector` contains debug events from **at least n distinct plugin stages**
6. Verify: All enabled plugins appear in the event stream

### Phase 4: Incremental Global Debug Dimension Accumulation

Start from all-off (all 13 plugins enabled, all per_plugin debug on). For iteration `n = 1, 2, 3, 4, 5`:

1. Enable global dimensions 1..n
2. Trigger request
3. Verify: `TraceCollector` contains debug events from the enabled dimensions
4. Verify: Disabled dimensions produce no debug events

### Phase 5: Full-On Conflict Detection

1. Enable **all 5 global debug dimensions**
2. Enable **all 13 plugins**
3. Enable **per_plugin debug** for all applicable plugins
4. Trigger request
5. Verify: No conflicts, crashes, or duplicate events — all 13 plugin debug events appear cleanly
6. Verify: No dimension overlap causes duplicate/wrong events

## Test Infrastructure

### Dependencies

- `starlette.testclient.TestClient`
- `aigateway_api.main.create_app`
- `aigateway_core.shared.trace_event.TraceCollector`
- `aigateway_core.shared.debug_config.DebugConfig`

### Request Template

```python
CHAT_REQUEST = {
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "..."}],
    "temperature": 0.7,
    "max_tokens": 100,
}
```

### TraceCollector Inspection

After each request, grab the active collector via `TraceCollector.current()` and inspect `collector.events` for:
- `event.kind == "debug"`
- `event.stage` matches the plugin/dimension under test
- `event.payload` contains expected details

### Config Management

All config mutations go through admin API endpoints (not direct file writes). After each test phase, reset to known baseline. Final teardown restores original `config.yaml`.

## Output

Markdown report at `docs/test/plugin_debug_test_report.md` with:
- Table per phase: plugin/dimension, enabled, debug event found, behavior verified, status
- Summary: pass/fail counts, any failures with details
