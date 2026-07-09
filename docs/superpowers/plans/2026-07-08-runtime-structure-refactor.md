# Runtime Structure Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize the gateway runtime so the real implementation lives under `prefix → dispatch → pipelines → route`, with core files actually moved and split into the agreed target directory structure rather than hidden behind re-export-only package layers.

**Architecture:** Move runtime ownership into `aigateway-core/src/aigateway_core/`, where `dispatch/` becomes the orchestration skeleton, `prefix/` holds shared pre-routing stages, `pipelines/` holds understanding and generation-specific chains, and `route/` holds unified downstream bridge/streaming/response closure. Keep `aigateway-api` as the HTTP surface by leaving only protocol adapters there, while old core root files become thin compatibility shims during the migration.

**Tech Stack:** Python 3, FastAPI, pytest, LiteLLM, Redis, Qdrant, Docker Compose

## Global Constraints

- Use the approved final runtime directory structure exactly: `prefix/`, `dispatch/`, `pipelines/understanding/`, `pipelines/generation/`, `route/`, `shared/`.
- This is a real file migration, not a re-export-only reorganization. The main implementations for `PipelineEngine`, `RequestDispatcher`, `LiteLLMBridge`, `PIIDetector`, `KeyStore`, and cache logic must live in the new paths.
- `aigateway-api` must remain the protocol surface; runtime skeleton ownership moves into `aigateway-core`.
- Old root-level files such as `pipeline.py`, `litellm_bridge.py`, `context.py`, `caching.py`, and `security.py` may remain only as thin compatibility shims.
- `security.py` must be split by responsibility: `PIIDetector` under `prefix/pii/`, `KeyStore` under `shared/auth/`.
- `caching.py` must be split by responsibility under `prefix/cache/`; do not just rename the file.
- After each code-changing task, run focused unit tests for that change.
- After the final migration, run full backend regression tests and an end-to-end full-chain verification so all interfaces work, imports resolve, and no API route errors.
- Follow repo workflow: commit after each code-changing task, rebuild affected Docker images when backend runtime code changes, and report verification results faithfully.
- Update `CLAUDE.md` if the final contributor-facing runtime structure guidance materially changes.

---

## File Structure Map

### New files

- `aigateway-core/src/aigateway_core/prefix/__init__.py`
  - Runtime prefix package marker.
- `aigateway-core/src/aigateway_core/prefix/cache/__init__.py`
  - Prefix cache package exports.
- `aigateway-core/src/aigateway_core/prefix/cache/cache_manager.py`
  - Main cache manager implementation extracted from `caching.py`.
- `aigateway-core/src/aigateway_core/prefix/cache/cache_keys.py`
  - Cache key v2 and prompt normalization helpers.
- `aigateway-core/src/aigateway_core/prefix/cache/l3_semantic.py`
  - L3 vector/backfill/search helpers extracted from API/core mixed locations.
- `aigateway-core/src/aigateway_core/prefix/pii/__init__.py`
  - Prefix PII package exports.
- `aigateway-core/src/aigateway_core/prefix/pii/detector.py`
  - `PIIDetector` main implementation.
- `aigateway-core/src/aigateway_core/shared/auth/__init__.py`
  - Shared auth package exports.
- `aigateway-core/src/aigateway_core/shared/auth/key_store.py`
  - `KeyStore` main implementation.
- `aigateway-core/src/aigateway_core/dispatch/__init__.py`
  - Dispatch package exports.
- `aigateway-core/src/aigateway_core/dispatch/context.py`
  - `PipelineContext` main implementation.
- `aigateway-core/src/aigateway_core/dispatch/pipeline_engine.py`
  - `PipelineEngine` and `Plugin` protocol main implementation.
- `aigateway-core/src/aigateway_core/dispatch/classifier.py`
  - `classify_request(...)` and modality helper functions.
- `aigateway-core/src/aigateway_core/dispatch/dispatcher.py`
  - `RequestDispatcher` main implementation.
- `aigateway-core/src/aigateway_core/route/__init__.py`
  - Route package exports.
- `aigateway-core/src/aigateway_core/route/bridge/__init__.py`
  - Route bridge package exports.
- `aigateway-core/src/aigateway_core/route/bridge/litellm_bridge.py`
  - `LiteLLMBridge` main implementation.
- `aigateway-core/src/aigateway_core/route/bridge/cooldown.py`
  - `ProviderCooldownTracker` extracted helper.
- `aigateway-core/src/aigateway_core/route/streaming/__init__.py`
  - Route streaming package exports.
- `aigateway-core/src/aigateway_core/route/streaming/sse.py`
  - `SSEGenerator` main implementation.
- `aigateway-core/src/aigateway_core/route/streaming/cache_stream.py`
  - `simulate_stream_from_cache(...)` implementation.
- `aigateway-core/src/aigateway_core/route/streaming/metrics_wrapper.py`
  - `_wrap_stream_for_metrics(...)` extracted from API surface.
- `aigateway-core/src/aigateway_core/route/metrics/__init__.py`
  - Route metrics package exports.
- `aigateway-core/src/aigateway_core/route/metrics/costing.py`
  - `_estimate_cost(...)` extracted from API surface.
- `tests/test_runtime_structure_dispatch_imports.py`
  - Verifies dispatch classes/functions resolve from new core paths.
- `tests/test_runtime_structure_prefix_split.py`
  - Verifies `PIIDetector`, `KeyStore`, and cache helpers resolve from split locations.
- `tests/test_runtime_structure_route_streaming.py`
  - Verifies streaming and bridge objects resolve from route-layer files.

### Existing files to modify

- `aigateway-core/src/aigateway_core/context.py`
  - Reduce to compatibility shim exporting `PipelineContext` from `dispatch/context.py`.
- `aigateway-core/src/aigateway_core/pipeline.py`
  - Reduce to compatibility shim exporting `PipelineEngine` and `Plugin` from `dispatch/pipeline_engine.py`.
- `aigateway-core/src/aigateway_core/caching.py`
  - Reduce to compatibility shim exporting split cache interfaces.
- `aigateway-core/src/aigateway_core/security.py`
  - Reduce to compatibility shim exporting `PIIDetector` and `KeyStore` from their new homes.
- `aigateway-core/src/aigateway_core/plugin_registry.py`
  - Update imports to follow relocated context/engine modules if needed.
- `aigateway-core/src/aigateway_core/litellm_bridge.py`
  - Reduce to compatibility shim exporting route bridge classes.
- `aigateway-core/src/aigateway_core/generation_optimization/...`
  - Update imports if they referenced old root-level modules.
- `aigateway-core/src/aigateway_core/media/...`
  - Update imports if they referenced old root-level modules.
- `aigateway-api/src/aigateway_api/dispatcher.py`
  - Reduce to API adapter importing the new core dispatcher/classifier.
- `aigateway-api/src/aigateway_api/openai_compat.py`
  - Keep protocol models/handlers; import extracted metrics/cache helpers from new core modules.
- `aigateway-api/src/aigateway_api/streaming.py`
  - Keep `create_sse_response(...)`; import `SSEGenerator` and `simulate_stream_from_cache(...)` from new route modules.
- `aigateway-api/src/aigateway_api/main.py`
  - Update app wiring/imports to instantiate runtime pieces from the new core locations.
- `tests/test_cache_key_v2.py`
  - Update imports if it references old cache module internals.
- `CLAUDE.md`
  - Update runtime structure guidance if the final contributor-facing layout changes materially.

## Task 1: Create dispatch package and move context/engine/classifier implementations

**Files:**
- Create: `aigateway-core/src/aigateway_core/dispatch/__init__.py`
- Create: `aigateway-core/src/aigateway_core/dispatch/context.py`
- Create: `aigateway-core/src/aigateway_core/dispatch/pipeline_engine.py`
- Create: `aigateway-core/src/aigateway_core/dispatch/classifier.py`
- Modify: `aigateway-core/src/aigateway_core/context.py`
- Modify: `aigateway-core/src/aigateway_core/pipeline.py`
- Test: `tests/test_runtime_structure_dispatch_imports.py`

**Interfaces:**
- Consumes: existing `PipelineContext`, `PipelineEngine`, `Plugin`, `classify_request(...)`, `_content_modality_hint(...)`, `_has_multimodal_content(...)`
- Produces:
  - `aigateway_core.dispatch.context.PipelineContext`
  - `aigateway_core.dispatch.pipeline_engine.PipelineEngine`
  - `aigateway_core.dispatch.pipeline_engine.Plugin`
  - `aigateway_core.dispatch.classifier.classify_request(body: Any, config_manager: Any) -> str`
  - `aigateway_core.dispatch.classifier._content_modality_hint(messages: list) -> Optional[str]`
  - `aigateway_core.dispatch.classifier._has_multimodal_content(messages: list) -> bool`

- [ ] **Step 1: Write the failing dispatch import tests**

```python
from aigateway_core.dispatch.classifier import classify_request
from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.dispatch.pipeline_engine import PipelineEngine


class DummyConfig:
    def get(self, key, default=None):
        if key != "providers":
            return default
        return {
            "mock": {
                "model_grouper": [
                    {
                        "models": [
                            {"name": "image-gen", "modalities": ["generative"]},
                            {"name": "text-model", "modalities": ["llm"]},
                        ]
                    }
                ]
            }
        }


def test_dispatch_modules_import_from_new_paths():
    ctx = PipelineContext(request={"messages": []}, pipeline_kind="understanding")
    engine = PipelineEngine(registry=None, pipeline_kind="understanding")
    assert ctx.pipeline_kind == "understanding"
    assert engine.pipeline_kind == "understanding"


def test_classify_request_prefers_generation_modalities():
    body = {
        "model": "image-gen",
        "messages": [{"role": "user", "content": "draw a cat"}],
    }
    assert classify_request(body, DummyConfig()) == "generation"
```

Save as `tests/test_runtime_structure_dispatch_imports.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_runtime_structure_dispatch_imports.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aigateway_core.dispatch'`.

- [ ] **Step 3: Create `dispatch/context.py` by copying the current `PipelineContext` implementation**

```python
from aigateway_core.context import PipelineContext

__all__ = ["PipelineContext"]
```

Use this temporary bootstrap only to get the file in place, then replace it in the next step with the full current implementation copied out of `aigateway_core/context.py` so the main class definition truly lives in `dispatch/context.py`.

- [ ] **Step 4: Replace the bootstrap with the full `PipelineContext` implementation**

Copy the current class body from `aigateway-core/src/aigateway_core/context.py` into `aigateway-core/src/aigateway_core/dispatch/context.py` and make the new file the defining location. Keep the class name unchanged:

```python
class PipelineContext:
    ...
```

Do not leave the real implementation in the old file.

- [ ] **Step 5: Create `dispatch/pipeline_engine.py` by moving the real engine implementation**

Move the current engine implementation from `aigateway-core/src/aigateway_core/pipeline.py` into the new file, preserving the public names:

```python
class Plugin(Protocol):
    ...


class PipelineEngine:
    ...
```

Keep imports aligned with the new context path:

```python
from .context import PipelineContext
from aigateway_core.plugin_registry import PluginRegistry
```

Update local imports further in the task once the compatibility shims exist.

- [ ] **Step 6: Create `dispatch/classifier.py` and move the real classifier helpers**

Move these functions out of `aigateway-api/src/aigateway_api/dispatcher.py` into the new file:

```python
def _has_multimodal_content(messages: list) -> bool:
    ...


def _content_modality_hint(messages: list) -> Optional[str]:
    ...


def classify_request(body: Any, config_manager: Any) -> str:
    ...
```

Preserve the current behavior exactly:
- `generation_intent == True` wins
- multimodal image/audio/video content maps to `generation`
- non-`auto` generative model names map to `generation`
- default is `understanding`

- [ ] **Step 7: Convert `context.py` into a thin compatibility shim**

Replace the file contents with:

```python
from aigateway_core.dispatch.context import PipelineContext

__all__ = ["PipelineContext"]
```

- [ ] **Step 8: Convert `pipeline.py` into a thin compatibility shim**

Replace the file contents with:

```python
from aigateway_core.dispatch.pipeline_engine import PipelineEngine, Plugin

__all__ = ["PipelineEngine", "Plugin"]
```

- [ ] **Step 9: Run the focused dispatch tests**

Run: `python3 -m pytest tests/test_runtime_structure_dispatch_imports.py -v`
Expected: PASS.

- [ ] **Step 10: Run broader regression for engine/context imports**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py --maxfail=1`
Expected: PASS or fail only on later migration gaps outside this task.

- [ ] **Step 11: Commit**

```bash
git add aigateway-core/src/aigateway_core/dispatch aigateway-core/src/aigateway_core/context.py aigateway-core/src/aigateway_core/pipeline.py tests/test_runtime_structure_dispatch_imports.py
git commit -m "refactor: move dispatch context and pipeline engine"
```

## Task 2: Move RequestDispatcher into core dispatch and leave API adapter behind

**Files:**
- Create: `aigateway-core/src/aigateway_core/dispatch/dispatcher.py`
- Modify: `aigateway-api/src/aigateway_api/dispatcher.py`
- Modify: `aigateway-api/src/aigateway_api/main.py`
- Test: `tests/test_runtime_structure_dispatch_imports.py`

**Interfaces:**
- Consumes:
  - `aigateway_core.dispatch.context.PipelineContext`
  - `aigateway_core.dispatch.classifier.classify_request(body: Any, config_manager: Any) -> str`
  - existing request orchestration logic from `aigateway_api.dispatcher.RequestDispatcher`
- Produces:
  - `aigateway_core.dispatch.dispatcher.RequestDispatcher`
  - `aigateway_api.dispatcher.RequestDispatcher` as adapter import alias

- [ ] **Step 1: Add a failing dispatcher-origin test**

Append to `tests/test_runtime_structure_dispatch_imports.py`:

```python
from aigateway_core.dispatch.dispatcher import RequestDispatcher as CoreRequestDispatcher
from aigateway_api.dispatcher import RequestDispatcher as ApiRequestDispatcher


def test_api_dispatcher_aliases_core_dispatcher():
    assert ApiRequestDispatcher is CoreRequestDispatcher
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `python3 -m pytest tests/test_runtime_structure_dispatch_imports.py::test_api_dispatcher_aliases_core_dispatcher -v`
Expected: FAIL because `aigateway_core.dispatch.dispatcher` does not exist yet.

- [ ] **Step 3: Move the real `RequestDispatcher` implementation into core**

Create `aigateway-core/src/aigateway_core/dispatch/dispatcher.py` and move the full current `RequestDispatcher` implementation from `aigateway-api/src/aigateway_api/dispatcher.py` into it. Keep helper functions that are tightly coupled to request orchestration in the same file for now, including:

```python
def _extract_cacheable_context(messages: list, tail: int = 3) -> list:
    ...


def _resolve_cache_scope(request: Request, pii_meta: Optional[dict]) -> str:
    ...


def _emit_stage(...):
    ...


def _emit_plugin(...):
    ...


class RequestDispatcher:
    ...
```

Update imports to use the new core classifier and context modules:

```python
from aigateway_core.dispatch.classifier import classify_request
from aigateway_core.dispatch.context import PipelineContext
```

- [ ] **Step 4: Reduce `aigateway_api/dispatcher.py` to an adapter**

Replace the file contents with:

```python
from aigateway_core.dispatch.classifier import classify_request
from aigateway_core.dispatch.dispatcher import RequestDispatcher

__all__ = ["RequestDispatcher", "classify_request"]
```

- [ ] **Step 5: Update `main.py` to import the dispatcher from core**

Find the `RequestDispatcher` import in `aigateway-api/src/aigateway_api/main.py` and switch it to:

```python
from aigateway_core.dispatch.dispatcher import RequestDispatcher
```

If `main.py` imports `classify_request`, switch it to the new core classifier module too.

- [ ] **Step 6: Run the focused dispatcher-origin test**

Run: `python3 -m pytest tests/test_runtime_structure_dispatch_imports.py::test_api_dispatcher_aliases_core_dispatcher -v`
Expected: PASS.

- [ ] **Step 7: Run backend regression focused on dispatcher behavior**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py --maxfail=1`
Expected: PASS or fail only on known later migration gaps.

- [ ] **Step 8: Commit**

```bash
git add aigateway-core/src/aigateway_core/dispatch/dispatcher.py aigateway-api/src/aigateway_api/dispatcher.py aigateway-api/src/aigateway_api/main.py tests/test_runtime_structure_dispatch_imports.py
git commit -m "refactor: move request dispatcher into core"
```

## Task 3: Split security and cache into prefix/shared real implementations

**Files:**
- Create: `aigateway-core/src/aigateway_core/prefix/cache/__init__.py`
- Create: `aigateway-core/src/aigateway_core/prefix/cache/cache_manager.py`
- Create: `aigateway-core/src/aigateway_core/prefix/cache/cache_keys.py`
- Create: `aigateway-core/src/aigateway_core/prefix/pii/__init__.py`
- Create: `aigateway-core/src/aigateway_core/prefix/pii/detector.py`
- Create: `aigateway-core/src/aigateway_core/shared/auth/__init__.py`
- Create: `aigateway-core/src/aigateway_core/shared/auth/key_store.py`
- Modify: `aigateway-core/src/aigateway_core/caching.py`
- Modify: `aigateway-core/src/aigateway_core/security.py`
- Modify: `tests/test_cache_key_v2.py`
- Test: `tests/test_runtime_structure_prefix_split.py`

**Interfaces:**
- Consumes: existing `CacheManager`, cache key helpers, `_normalize_prompt`, `PIIDetector`, `KeyStore`
- Produces:
  - `aigateway_core.prefix.cache.cache_manager.CacheManager`
  - `aigateway_core.prefix.cache.cache_keys._normalize_prompt(text: str) -> str`
  - cache-key helper functions currently exported from `caching.py`
  - `aigateway_core.prefix.pii.detector.PIIDetector`
  - `aigateway_core.shared.auth.key_store.KeyStore`

- [ ] **Step 1: Write the failing prefix split tests**

```python
from aigateway_core.prefix.cache.cache_keys import _normalize_prompt
from aigateway_core.prefix.cache.cache_manager import CacheManager
from aigateway_core.prefix.pii.detector import PIIDetector
from aigateway_core.shared.auth.key_store import KeyStore


def test_prefix_cache_and_security_modules_resolve_from_new_paths():
    assert _normalize_prompt("a   b") == "a b"
    assert PIIDetector.__name__ == "PIIDetector"
    assert KeyStore.__name__ == "KeyStore"
    assert CacheManager.__name__ == "CacheManager"
```

Save as `tests/test_runtime_structure_prefix_split.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_runtime_structure_prefix_split.py -v`
Expected: FAIL with `ModuleNotFoundError` for the new prefix/shared paths.

- [ ] **Step 3: Create `cache_keys.py` and move prompt normalization/key helpers there**

Move the real normalization and cache-key helper functions from `aigateway-core/src/aigateway_core/caching.py` into `aigateway-core/src/aigateway_core/prefix/cache/cache_keys.py`. Preserve their current signatures exactly, including the private helper name used by tests:

```python
def _normalize_prompt(text: str) -> str:
    ...
```

Also move the cache-key v2 helper functions used by `tests/test_cache_key_v2.py` into the same file.

- [ ] **Step 4: Create `cache_manager.py` and move the real `CacheManager` implementation there**

Move the full `CacheManager` implementation out of `aigateway-core/src/aigateway_core/caching.py` into `aigateway-core/src/aigateway_core/prefix/cache/cache_manager.py`.

Keep any internal imports aligned to the new file layout:

```python
from .cache_keys import _normalize_prompt
```

- [ ] **Step 5: Create `detector.py` and move the real `PIIDetector` implementation there**

Move the current `PIIDetector` class body out of `aigateway-core/src/aigateway_core/security.py` into `aigateway-core/src/aigateway_core/prefix/pii/detector.py`.

- [ ] **Step 6: Create `key_store.py` and move the real `KeyStore` implementation there**

Move the current `KeyStore` class body out of `aigateway-core/src/aigateway_core/security.py` into `aigateway-core/src/aigateway_core/shared/auth/key_store.py`.

- [ ] **Step 7: Turn `caching.py` into a compatibility shim**

Replace the file contents with compatibility exports from the new split files. Use the exact names that existing callers still rely on, for example:

```python
from aigateway_core.prefix.cache.cache_manager import CacheManager
from aigateway_core.prefix.cache.cache_keys import _normalize_prompt

__all__ = ["CacheManager", "_normalize_prompt"]
```

Also re-export any other cache key helpers that current callers or tests import from `caching.py`.

- [ ] **Step 8: Turn `security.py` into a compatibility shim**

Replace the file contents with:

```python
from aigateway_core.prefix.pii.detector import PIIDetector
from aigateway_core.shared.auth.key_store import KeyStore

__all__ = ["PIIDetector", "KeyStore"]
```

- [ ] **Step 9: Update cache tests to import through the new cache-key file where appropriate**

If `tests/test_cache_key_v2.py` imports helper functions directly from `aigateway_core.caching`, switch those imports to `aigateway_core.prefix.cache.cache_keys` for the specific helpers now defined there.

- [ ] **Step 10: Run focused prefix/cache tests**

Run: `python3 -m pytest tests/test_runtime_structure_prefix_split.py tests/test_cache_key_v2.py -v`
Expected: PASS.

- [ ] **Step 11: Run broader backend regression**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py --maxfail=1`
Expected: PASS or fail only on later migration tasks.

- [ ] **Step 12: Commit**

```bash
git add aigateway-core/src/aigateway_core/prefix aigateway-core/src/aigateway_core/shared/auth aigateway-core/src/aigateway_core/caching.py aigateway-core/src/aigateway_core/security.py tests/test_runtime_structure_prefix_split.py tests/test_cache_key_v2.py
git commit -m "refactor: split prefix cache and security modules"
```

## Task 4: Move LiteLLM bridge into route/bridge and keep compatibility shim

**Files:**
- Create: `aigateway-core/src/aigateway_core/route/__init__.py`
- Create: `aigateway-core/src/aigateway_core/route/bridge/__init__.py`
- Create: `aigateway-core/src/aigateway_core/route/bridge/cooldown.py`
- Create: `aigateway-core/src/aigateway_core/route/bridge/litellm_bridge.py`
- Modify: `aigateway-core/src/aigateway_core/litellm_bridge.py`
- Modify: `aigateway-api/src/aigateway_api/main.py`
- Test: `tests/test_runtime_structure_route_streaming.py`

**Interfaces:**
- Consumes: existing `LiteLLMBridge`, `ProviderCooldownTracker`
- Produces:
  - `aigateway_core.route.bridge.cooldown.ProviderCooldownTracker`
  - `aigateway_core.route.bridge.litellm_bridge.LiteLLMBridge`
  - compatibility export from `aigateway_core.litellm_bridge`

- [ ] **Step 1: Write the failing route bridge tests**

```python
from aigateway_core.route.bridge.cooldown import ProviderCooldownTracker
from aigateway_core.route.bridge.litellm_bridge import LiteLLMBridge


def test_route_bridge_objects_resolve_from_new_paths():
    assert ProviderCooldownTracker.__name__ == "ProviderCooldownTracker"
    assert LiteLLMBridge.__name__ == "LiteLLMBridge"
```

Save as `tests/test_runtime_structure_route_streaming.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_runtime_structure_route_streaming.py::test_route_bridge_objects_resolve_from_new_paths -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Create `cooldown.py` and move `ProviderCooldownTracker` there**

Move the full class implementation out of `aigateway-core/src/aigateway_core/litellm_bridge.py` into:

```python
class ProviderCooldownTracker:
    ...
```

Save it in `aigateway-core/src/aigateway_core/route/bridge/cooldown.py`.

- [ ] **Step 4: Create `route/bridge/litellm_bridge.py` and move `LiteLLMBridge` there**

Move the full `LiteLLMBridge` implementation out of the root file into the new route bridge file. Import the cooldown tracker from the new module:

```python
from .cooldown import ProviderCooldownTracker
```

- [ ] **Step 5: Turn the old root bridge file into a compatibility shim**

Replace `aigateway-core/src/aigateway_core/litellm_bridge.py` with:

```python
from aigateway_core.route.bridge.cooldown import ProviderCooldownTracker
from aigateway_core.route.bridge.litellm_bridge import LiteLLMBridge

__all__ = ["LiteLLMBridge", "ProviderCooldownTracker"]
```

- [ ] **Step 6: Update backend wiring to import the bridge from the new route path**

In `aigateway-api/src/aigateway_api/main.py`, switch the bridge import to:

```python
from aigateway_core.route.bridge.litellm_bridge import LiteLLMBridge
```

- [ ] **Step 7: Run the focused route bridge test**

Run: `python3 -m pytest tests/test_runtime_structure_route_streaming.py::test_route_bridge_objects_resolve_from_new_paths -v`
Expected: PASS.

- [ ] **Step 8: Run backend regression**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py --maxfail=1`
Expected: PASS or fail only on later route/streaming migration gaps.

- [ ] **Step 9: Commit**

```bash
git add aigateway-core/src/aigateway_core/route aigateway-core/src/aigateway_core/litellm_bridge.py aigateway-api/src/aigateway_api/main.py tests/test_runtime_structure_route_streaming.py
git commit -m "refactor: move litellm bridge into route layer"
```

## Task 5: Move streaming logic into route/streaming and keep FastAPI adapter in API surface

**Files:**
- Create: `aigateway-core/src/aigateway_core/route/streaming/__init__.py`
- Create: `aigateway-core/src/aigateway_core/route/streaming/sse.py`
- Create: `aigateway-core/src/aigateway_core/route/streaming/cache_stream.py`
- Create: `aigateway-core/src/aigateway_core/route/streaming/metrics_wrapper.py`
- Create: `aigateway-core/src/aigateway_core/route/metrics/__init__.py`
- Create: `aigateway-core/src/aigateway_core/route/metrics/costing.py`
- Modify: `aigateway-api/src/aigateway_api/streaming.py`
- Modify: `aigateway-api/src/aigateway_api/openai_compat.py`
- Test: `tests/test_runtime_structure_route_streaming.py`

**Interfaces:**
- Consumes: existing `SSEGenerator`, `simulate_stream_from_cache(...)`, `_wrap_stream_for_metrics(...)`, `_estimate_cost(...)`
- Produces:
  - `aigateway_core.route.streaming.sse.SSEGenerator`
  - `aigateway_core.route.streaming.cache_stream.simulate_stream_from_cache(response_json: str, chunk_delay_ms: int = 20, chunk_count: int = 20, hit_tier: str = "L1") -> AsyncIterator[Dict[str, Any]]`
  - `aigateway_core.route.streaming.metrics_wrapper._wrap_stream_for_metrics(...)`
  - `aigateway_core.route.metrics.costing._estimate_cost(model: str, total_tokens: int) -> float`
  - `aigateway_api.streaming.create_sse_response(...)` remains surface adapter

- [ ] **Step 1: Add failing streaming-origin tests**

Append to `tests/test_runtime_structure_route_streaming.py`:

```python
from aigateway_core.route.metrics.costing import _estimate_cost
from aigateway_core.route.streaming.cache_stream import simulate_stream_from_cache
from aigateway_core.route.streaming.sse import SSEGenerator


def test_route_streaming_helpers_resolve_from_new_paths():
    assert SSEGenerator.__name__ == "SSEGenerator"
    assert callable(simulate_stream_from_cache)
    assert _estimate_cost("gpt-4o", 1000) > 0
```

- [ ] **Step 2: Run focused tests to verify they fail**

Run: `python3 -m pytest tests/test_runtime_structure_route_streaming.py -v`
Expected: FAIL because the new route streaming modules do not exist yet.

- [ ] **Step 3: Create `sse.py` and move `SSEGenerator` there**

Move the full class body from `aigateway-api/src/aigateway_api/streaming.py` into `aigateway-core/src/aigateway_core/route/streaming/sse.py`:

```python
class SSEGenerator:
    ...
```

- [ ] **Step 4: Create `cache_stream.py` and move `simulate_stream_from_cache(...)` there**

Move the full current implementation of `simulate_stream_from_cache(...)` out of `aigateway-api/src/aigateway_api/streaming.py` into the new route file.

- [ ] **Step 5: Create `metrics_wrapper.py` and `costing.py` from the API runtime helpers**

Move the current helper bodies from `aigateway-api/src/aigateway_api/openai_compat.py` into the new files, preserving signatures exactly:

```python
async def _wrap_stream_for_metrics(completion_gen: Any, metrics_collector: Any, model: str, user_id: str = "") -> Any:
    ...
```

```python
def _estimate_cost(model: str, total_tokens: int) -> float:
    ...
```

- [ ] **Step 6: Reduce API `streaming.py` to a FastAPI adapter**

Keep only the `StreamingResponse` wrapper and import `SSEGenerator` from the core route module:

```python
from aigateway_core.route.streaming.sse import SSEGenerator
```

Retain:

```python
def create_sse_response(completion_gen, chat_id=None) -> StreamingResponse:
    ...
```

Remove the moved core logic from this file.

- [ ] **Step 7: Update `openai_compat.py` to import the moved helpers**

Replace direct definitions with imports:

```python
from aigateway_core.route.metrics.costing import _estimate_cost
from aigateway_core.route.streaming.metrics_wrapper import _wrap_stream_for_metrics
```

Also import `simulate_stream_from_cache` from `aigateway_core.route.streaming.cache_stream` wherever the API surface still needs it.

- [ ] **Step 8: Run focused route streaming tests**

Run: `python3 -m pytest tests/test_runtime_structure_route_streaming.py -v`
Expected: PASS.

- [ ] **Step 9: Run backend regression**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py --maxfail=1`
Expected: PASS or fail only on later import cleanup not covered by this task.

- [ ] **Step 10: Commit**

```bash
git add aigateway-core/src/aigateway_core/route/streaming aigateway-core/src/aigateway_core/route/metrics aigateway-api/src/aigateway_api/streaming.py aigateway-api/src/aigateway_api/openai_compat.py tests/test_runtime_structure_route_streaming.py
git commit -m "refactor: move route streaming helpers into core"
```

## Task 6: Repoint runtime imports across core packages to the new skeleton

**Files:**
- Modify: `aigateway-core/src/aigateway_core/plugin_registry.py`
- Modify: `aigateway-core/src/aigateway_core/generation_optimization/**/*.py`
- Modify: `aigateway-core/src/aigateway_core/media/**/*.py`
- Modify: `aigateway-api/src/aigateway_api/main.py`
- Modify: `aigateway-api/src/aigateway_api/openai_compat.py`
- Modify: any failing import sites surfaced by the regression suite
- Test: existing backend test files that fail due to stale imports

**Interfaces:**
- Consumes: all moved modules from Tasks 1-5
- Produces: import graph that prefers the new runtime skeleton and leaves old files as compatibility shims only

- [ ] **Step 1: Run the backend suite to surface stale imports**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py --maxfail=20`
Expected: FAIL with stale import paths or circular import issues that identify remaining callers to update.

- [ ] **Step 2: Update direct imports from old root modules to new paths**

For each stale import surfaced in Step 1, replace direct imports of old defining modules with the new owning module. Apply these patterns:

```python
from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.dispatch.pipeline_engine import PipelineEngine
from aigateway_core.dispatch.dispatcher import RequestDispatcher
from aigateway_core.prefix.cache.cache_manager import CacheManager
from aigateway_core.prefix.pii.detector import PIIDetector
from aigateway_core.shared.auth.key_store import KeyStore
from aigateway_core.route.bridge.litellm_bridge import LiteLLMBridge
```

Prefer the new owning modules over the old shim files everywhere you touch.

- [ ] **Step 3: Resolve any circular imports introduced by the move**

When a stale import update creates a cycle, break it by moving shared helper imports to the narrowest owning module or by converting local imports inside functions that are only needed at runtime. Do not move main implementations back to the old root files.

- [ ] **Step 4: Re-run the backend suite until import regressions stop appearing**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py --maxfail=20`
Expected: import-related failures are gone; any remaining failures are behavior regressions, not missing-module errors.

- [ ] **Step 5: Commit**

```bash
git add aigateway-core/src/aigateway_core aigateway-api/src/aigateway_api
git commit -m "refactor: repoint runtime imports to new skeleton"
```

## Task 7: Final backend regression and Docker verification

**Files:**
- Modify: any files required to fix remaining runtime regressions found by the full suite
- Test: `tests/`

**Interfaces:**
- Consumes: migrated runtime skeleton from Tasks 1-6
- Produces: passing backend regression suite and healthy rebuilt gateway container

- [ ] **Step 1: Run the full backend regression suite**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py`
Expected: PASS.

- [ ] **Step 2: Fix any remaining behavior regressions surfaced by the full suite**

If Step 1 fails, fix the specific failing behavior in the owning new modules rather than adding new logic to old shim files. Keep the fixes scoped to the reported regressions.

- [ ] **Step 3: Re-run the full backend regression suite**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py`
Expected: PASS.

- [ ] **Step 4: Rebuild the backend image and verify health**

Run:

```bash
sudo DOCKER_BUILDKIT=1 docker compose up -d --build gateway
curl -sf localhost:8000/health
docker compose logs --tail=50 gateway | grep -i error
```

Expected:
- Docker build succeeds.
- `/health` returns success.
- Gateway logs show no new meaningful runtime import/initialization errors.

- [ ] **Step 5: Commit any final backend regression fixes**

```bash
git add aigateway-core/src/aigateway_core aigateway-api/src/aigateway_api tests
git commit -m "fix: stabilize runtime structure regression suite"
```

Skip this commit if Steps 1-4 passed without any new code changes after Task 6.

## Task 8: Full-chain API verification and docs sync

**Files:**
- Modify: `CLAUDE.md` (if runtime structure guidance now needs current-state updates)
- Test: live gateway endpoints after rebuild

**Interfaces:**
- Consumes: passing backend runtime from Task 7
- Produces: verified API surface and updated contributor docs

- [ ] **Step 1: Verify non-streaming chat completions**

Run:

```bash
curl -sS http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: test-key' \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "hello"}],
    "stream": false
  }'
```

Expected: HTTP 200 JSON response or a known provider/auth failure envelope from downstream, but no gateway-side import error, traceback, or malformed response envelope.

- [ ] **Step 2: Verify streaming chat completions**

Run:

```bash
curl -N http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: test-key' \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "hello"}],
    "stream": true
  }'
```

Expected:
- SSE output begins with `data:` lines.
- Stream closes cleanly with `[DONE]`.
- No gateway-side import/runtime exception appears in server logs.

- [ ] **Step 3: Verify admin/health surfaces still load**

Run:

```bash
curl -sf http://localhost:8000/health
curl -sS http://localhost:8000/metrics | head
```

Expected:
- `/health` succeeds.
- `/metrics` responds with Prometheus text.

- [ ] **Step 4: Update `CLAUDE.md` if the runtime structure guidance should now describe the new current-state ownership**

If the migration materially changed contributor navigation, add a concise current-state note under the Architecture or Key Files guidance. Use a short fact-only update like:

```md
- **Runtime skeleton now lives under `aigateway_core/{prefix,dispatch,pipelines,route}`** — root files like `pipeline.py`, `caching.py`, `security.py`, and `litellm_bridge.py` are compatibility shims; new work should target the owning subpackages.
```

Before editing, run `wc -l CLAUDE.md` and keep the file under the repo guideline cap.

- [ ] **Step 5: Commit docs sync if needed**

```bash
git add CLAUDE.md
git commit -m "docs: update runtime structure guidance"
```

Skip this commit if `CLAUDE.md` is unchanged.

## Self-Review Checklist

- Spec coverage:
  - real implementation moves for `PipelineEngine`, `RequestDispatcher`, `LiteLLMBridge`, `PIIDetector`, `KeyStore`, and cache logic → Tasks 1-5
  - old root files downgraded to compatibility shims → Tasks 1-5
  - API surface retained while runtime ownership moves to core → Tasks 2 and 5
  - focused unit tests after each change → Tasks 1-6
  - final backend regression + Docker rebuild → Task 7
  - final full-chain interface verification → Task 8
- Placeholder scan:
  - no `TODO`/`TBD` placeholders remain
  - all code-changing tasks include concrete test commands and expected outcomes
- Type consistency:
  - `PipelineContext`, `PipelineEngine`, `Plugin`, `RequestDispatcher`, `LiteLLMBridge`, `ProviderCooldownTracker`, `PIIDetector`, and `KeyStore` keep their existing public names
  - helper function signatures for `classify_request`, `_normalize_prompt`, `simulate_stream_from_cache`, `_wrap_stream_for_metrics`, and `_estimate_cost` remain stable during the move

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-08-runtime-structure-refactor.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
