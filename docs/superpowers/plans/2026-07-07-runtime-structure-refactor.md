# Runtime Structure Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize `aigateway-core/src/aigateway_core/` into the 总分总 runtime skeleton (`prefix/` → `dispatch/` → `pipelines/{understanding,generation}/` → `route/` + `shared/`) without changing runtime behavior.

**Architecture:** Move each classified module into its target subpackage in small, independently testable batches. Each old module is turned into a **re-export shim** so existing import paths keep working while new-path imports become the canonical form. The API/CLI/Panel surfaces stay untouched.

**Tech Stack:** Python 3.12, pytest, FastAPI (import surface only), no new dependencies.

## Global Constraints

- **Zero behavior change.** All existing tests must keep passing after every task. Do not rename symbols, do not change function signatures, do not tweak semantics.
- **Backward-compatible imports.** Old paths like `aigateway_core.caching`, `aigateway_core.security`, `aigateway_core.pipeline`, `aigateway_core.litellm_bridge`, `aigateway_core.plugins.*`, `aigateway_core.generation_optimization.*`, `aigateway_core.media.*` MUST keep resolving to the same symbols. Add `# Backward-compat re-export ...` shims; do not delete the old files in this refactor.
- **New canonical paths are additive.** The runtime skeleton is introduced under new subpackages; no file already outside `aigateway_core` is moved.
- **Surfaces are off-limits.** Do not edit `aigateway-api/`, `aigateway-cli/`, `control-panel/`, `tests/`, `scripts/`, `docker-compose.yml`, `config.yaml`, or `Dockerfile*` beyond what a single explicit task calls out. This refactor is core-only.
- **Full test suite command:** `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py` (per `CLAUDE.md`).
- **Commit style:** conventional (`refactor:` for moves, `test:` for tests, `docs:` for docs), one commit per task step marked "Commit".
- **No docker rebuild required** for this refactor (all edits are Python source under editable-installed packages, no baked-in structural config changes). Skip the rebuild rule from `CLAUDE.md`.
- **Design source of truth:** `docs/superpowers/specs/2026-07-07-runtime-structure-design.md`. If a step contradicts it, stop and ask.

---

## File Structure — Target Skeleton

Everything below lives inside `aigateway-core/src/aigateway_core/`. Existing top-level modules stay in place as re-export shims; new subpackages become the canonical home.

```text
aigateway_core/
  __init__.py                    # unchanged
  prefix/                        # NEW: shared pre-routing layer
    __init__.py
    pii/
      __init__.py                # re-exports from aigateway_core.security + pipeline.PIIDetectorPlugin
    cache/
      __init__.py                # re-exports from aigateway_core.caching + pipeline.PromptCachePlugin/SemanticCachePlugin
    media/
      __init__.py                # re-exports aigateway_core.media package surface

  dispatch/                      # NEW: total-orchestration layer
    __init__.py
    # canonical home is aigateway_api.dispatcher today; core-side stub exposes PipelineContext helpers.
    context.py                   # re-exports PipelineContext from aigateway_core.context

  pipelines/
    __init__.py
    understanding/
      __init__.py
      rag/
        __init__.py              # re-exports aigateway_core.plugins.rag_retriever_plugin
      conversation/
        __init__.py              # re-exports aigateway_core.plugins.conv_compressor_plugin
      compression/
        __init__.py              # re-exports PromptCompressPlugin from aigateway_core.pipeline

    generation/
      __init__.py
      director/
        __init__.py              # re-exports generation_optimization.strategies.ai_director + plugins.ai_director_plugin
      intent/
        __init__.py              # re-exports intent_evaluator strategy + plugin
      token/
        __init__.py              # re-exports token_compressor + feature_cache + prompt_confirmation + prompt_template_manager + video_preview
      draft/
        __init__.py              # re-exports draft_generator + draft plugin
      cost/
        __init__.py              # re-exports cost_tracker_plugin + generation_optimization.metrics + models + api_key_groups
      routing_signals/
        __init__.py              # re-exports gen_model_router_plugin (generation-side routing signal)

  route/                         # NEW: unified routing + response closure
    __init__.py
    model_resolution/
      __init__.py                # re-exports generation_optimization.strategies.model_router.ModelRouterStrategy
    bridge/
      __init__.py                # re-exports aigateway_core.litellm_bridge

  shared/                        # NEW: cross-layer utilities only
    __init__.py
    config.py                    # re-exports aigateway_core.config
    tracing.py                   # re-exports aigateway_core.tracing
    trace_event.py               # re-exports aigateway_core.trace_event
    exceptions.py                # re-exports aigateway_core.exceptions
    plugin_registry.py           # re-exports aigateway_core.plugin_registry
    logger.py                    # re-exports aigateway_core.logger
    metrics.py                   # re-exports aigateway_core.metrics
    debug_config.py              # re-exports aigateway_core.debug_config
    redis_client.py              # re-exports aigateway_core.redis_client
    qdrant_client.py             # re-exports aigateway_core.qdrant_client
    integration_configs.py       # re-exports aigateway_core.integration_configs

  # Existing files remain as authoritative implementation homes.
  # They are NOT deleted in this refactor.
  caching.py
  security.py
  pipeline.py
  litellm_bridge.py
  context.py
  config.py
  logger.py
  metrics.py
  tracing.py
  trace_event.py
  plugin_registry.py
  debug_config.py
  exceptions.py
  integration_configs.py
  redis_client.py
  qdrant_client.py
  plugins/
    conv_compressor_plugin.py
    rag_retriever_plugin.py
  media/…                        # unchanged
  generation_optimization/…      # unchanged
  code_rag/…                     # unchanged (out of scope for this refactor)
```

**Why re-exports and not moves?** The design spec explicitly recommends a phased migration with import bridges (Design §Recommended Migration Strategy, Phase 2). This plan implements Phase 1 (classification) and Phase 2 (target directories + adapters). Phases 3–4 (surface cleanup, runtime execution alignment) are out of scope for this plan.

---

## Task Overview

| # | Task | Deliverable |
|---|------|-------------|
| 1 | `shared/` skeleton | Cross-layer utilities re-exported under `aigateway_core.shared.*` |
| 2 | `prefix/` skeleton | PII, cache, media re-exported under `aigateway_core.prefix.*` |
| 3 | `dispatch/` skeleton | `PipelineContext` re-exported under `aigateway_core.dispatch.*` |
| 4 | `pipelines/understanding/` skeleton | RAG, conversation, compression re-exports |
| 5 | `pipelines/generation/` skeleton | Director/intent/token/draft/cost/routing_signals re-exports |
| 6 | `route/` skeleton | Model resolution + LiteLLM bridge re-exports |
| 7 | Runtime map doc + CLAUDE.md pointer | `docs/RUNTIME_MAP.md` and one-line pointer in `CLAUDE.md` |

Each task is independently testable and independently commit-able. Tasks 1–6 have the same shape: create a subpackage, populate `__init__.py` with `from X import Y` re-exports, write a smoke test that asserts import equivalence, run the full suite, commit.

---

## Task 1: `shared/` — cross-layer utilities

**Files:**
- Create: `aigateway-core/src/aigateway_core/shared/__init__.py`
- Create: `aigateway-core/src/aigateway_core/shared/config.py`
- Create: `aigateway-core/src/aigateway_core/shared/tracing.py`
- Create: `aigateway-core/src/aigateway_core/shared/trace_event.py`
- Create: `aigateway-core/src/aigateway_core/shared/exceptions.py`
- Create: `aigateway-core/src/aigateway_core/shared/plugin_registry.py`
- Create: `aigateway-core/src/aigateway_core/shared/logger.py`
- Create: `aigateway-core/src/aigateway_core/shared/metrics.py`
- Create: `aigateway-core/src/aigateway_core/shared/debug_config.py`
- Create: `aigateway-core/src/aigateway_core/shared/redis_client.py`
- Create: `aigateway-core/src/aigateway_core/shared/qdrant_client.py`
- Create: `aigateway-core/src/aigateway_core/shared/integration_configs.py`
- Test: `tests/test_runtime_skeleton_shared.py`

**Interfaces:**
- Consumes: existing modules `aigateway_core.config`, `aigateway_core.tracing`, `aigateway_core.trace_event`, `aigateway_core.exceptions`, `aigateway_core.plugin_registry`, `aigateway_core.logger`, `aigateway_core.metrics`, `aigateway_core.debug_config`, `aigateway_core.redis_client`, `aigateway_core.qdrant_client`, `aigateway_core.integration_configs`
- Produces: identical symbols under `aigateway_core.shared.<module>`. Every symbol accessible via the old path is also accessible via the new path and refers to the **same object** (`is`-identity, not just equal).

- [ ] **Step 1: Write the failing test**

Create `tests/test_runtime_skeleton_shared.py`:

```python
"""Verify aigateway_core.shared re-exports the same objects as the legacy paths."""
import importlib


PAIRS = [
    ("aigateway_core.config", "aigateway_core.shared.config"),
    ("aigateway_core.tracing", "aigateway_core.shared.tracing"),
    ("aigateway_core.trace_event", "aigateway_core.shared.trace_event"),
    ("aigateway_core.exceptions", "aigateway_core.shared.exceptions"),
    ("aigateway_core.plugin_registry", "aigateway_core.shared.plugin_registry"),
    ("aigateway_core.logger", "aigateway_core.shared.logger"),
    ("aigateway_core.metrics", "aigateway_core.shared.metrics"),
    ("aigateway_core.debug_config", "aigateway_core.shared.debug_config"),
    ("aigateway_core.redis_client", "aigateway_core.shared.redis_client"),
    ("aigateway_core.qdrant_client", "aigateway_core.shared.qdrant_client"),
    ("aigateway_core.integration_configs", "aigateway_core.shared.integration_configs"),
]


def test_shared_reexports_are_identical():
    for old, new in PAIRS:
        old_mod = importlib.import_module(old)
        new_mod = importlib.import_module(new)
        exported = [name for name in dir(old_mod) if not name.startswith("_")]
        assert exported, f"{old} has no public names to re-export"
        for name in exported:
            assert hasattr(new_mod, name), f"{new} missing {name!r} re-exported from {old}"
            assert getattr(new_mod, name) is getattr(old_mod, name), (
                f"{new}.{name} is not the same object as {old}.{name}"
            )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_runtime_skeleton_shared.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aigateway_core.shared'`.

- [ ] **Step 3: Create the `shared/` package**

Create `aigateway-core/src/aigateway_core/shared/__init__.py`:

```python
"""Cross-layer utilities.

Re-export shim for the 总分总 runtime skeleton (see
``docs/superpowers/specs/2026-07-07-runtime-structure-design.md``).
The authoritative implementations still live at ``aigateway_core.<module>``;
this package exposes them under their runtime-layer home.
"""
```

Create `aigateway-core/src/aigateway_core/shared/config.py`:

```python
"""Backward-compat re-export: config utilities live in the shared layer."""
from aigateway_core.config import *  # noqa: F401,F403
from aigateway_core.config import __all__ as _wrapped_all  # type: ignore[attr-defined]

__all__ = list(_wrapped_all) if isinstance(_wrapped_all, (list, tuple)) else []
```

If `aigateway_core.config` does not define `__all__`, use this variant instead (repeat the same pattern for every other file listed below):

```python
"""Backward-compat re-export: config utilities live in the shared layer."""
from aigateway_core import config as _wrapped

_public = [name for name in dir(_wrapped) if not name.startswith("_")]
for _name in _public:
    globals()[_name] = getattr(_wrapped, _name)

__all__ = _public
del _wrapped, _public, _name
```

Repeat the same pattern for every other file:

- `aigateway-core/src/aigateway_core/shared/tracing.py` → wraps `aigateway_core.tracing`
- `aigateway-core/src/aigateway_core/shared/trace_event.py` → wraps `aigateway_core.trace_event`
- `aigateway-core/src/aigateway_core/shared/exceptions.py` → wraps `aigateway_core.exceptions`
- `aigateway-core/src/aigateway_core/shared/plugin_registry.py` → wraps `aigateway_core.plugin_registry`
- `aigateway-core/src/aigateway_core/shared/logger.py` → wraps `aigateway_core.logger`
- `aigateway-core/src/aigateway_core/shared/metrics.py` → wraps `aigateway_core.metrics`
- `aigateway-core/src/aigateway_core/shared/debug_config.py` → wraps `aigateway_core.debug_config`
- `aigateway-core/src/aigateway_core/shared/redis_client.py` → wraps `aigateway_core.redis_client`
- `aigateway-core/src/aigateway_core/shared/qdrant_client.py` → wraps `aigateway_core.qdrant_client`
- `aigateway-core/src/aigateway_core/shared/integration_configs.py` → wraps `aigateway_core.integration_configs`

Each file's body is identical except for the `import ... as _wrapped` line and the leading docstring naming which layer it belongs to.

- [ ] **Step 4: Run smoke test to verify it passes**

Run: `python3 -m pytest tests/test_runtime_skeleton_shared.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite to verify no regression**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py`
Expected: same pass count as before Task 1 (no failures introduced).

- [ ] **Step 6: Commit**

```bash
git add aigateway-core/src/aigateway_core/shared/ tests/test_runtime_skeleton_shared.py
git commit -m "refactor: add aigateway_core.shared re-export layer"
```

---

## Task 2: `prefix/` — shared pre-routing layer

**Files:**
- Create: `aigateway-core/src/aigateway_core/prefix/__init__.py`
- Create: `aigateway-core/src/aigateway_core/prefix/pii/__init__.py`
- Create: `aigateway-core/src/aigateway_core/prefix/cache/__init__.py`
- Create: `aigateway-core/src/aigateway_core/prefix/media/__init__.py`
- Test: `tests/test_runtime_skeleton_prefix.py`

**Interfaces:**
- Consumes: `aigateway_core.security` (KeyStore, PIIDetector, …), `aigateway_core.caching` (CacheManager, cache key helpers, L3CleanupScheduler), `aigateway_core.media` (MediaCacheManager, MediaOptimizationPlugin, and the whole `media` subpackage), plus `aigateway_core.pipeline` classes `PIIDetectorPlugin`, `PromptCachePlugin`, `SemanticCachePlugin`.
- Produces:
  - `aigateway_core.prefix.pii.PIIDetectorPlugin` and every public name from `aigateway_core.security` re-exported under `aigateway_core.prefix.pii`.
  - `aigateway_core.prefix.cache.PromptCachePlugin`, `aigateway_core.prefix.cache.SemanticCachePlugin`, and every public name from `aigateway_core.caching` re-exported under `aigateway_core.prefix.cache`.
  - Every public name from `aigateway_core.media` re-exported under `aigateway_core.prefix.media`, and `aigateway_core.prefix.media.plugin` re-exporting `aigateway_core.media.plugin.MediaOptimizationPlugin`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_runtime_skeleton_prefix.py`:

```python
"""Verify aigateway_core.prefix re-exports match legacy paths."""
import importlib


def _assert_identical(new_mod, sources):
    for src_path in sources:
        src_mod = importlib.import_module(src_path)
        for name in dir(src_mod):
            if name.startswith("_"):
                continue
            assert hasattr(new_mod, name), (
                f"{new_mod.__name__} missing {name!r} from {src_path}"
            )
            assert getattr(new_mod, name) is getattr(src_mod, name), (
                f"{new_mod.__name__}.{name} diverges from {src_path}.{name}"
            )


def test_prefix_pii_reexports():
    from aigateway_core import prefix
    from aigateway_core.pipeline import PIIDetectorPlugin as LegacyPIIPlugin

    assert prefix.pii.PIIDetectorPlugin is LegacyPIIPlugin
    _assert_identical(prefix.pii, ["aigateway_core.security"])


def test_prefix_cache_reexports():
    from aigateway_core import prefix
    from aigateway_core.pipeline import PromptCachePlugin, SemanticCachePlugin

    assert prefix.cache.PromptCachePlugin is PromptCachePlugin
    assert prefix.cache.SemanticCachePlugin is SemanticCachePlugin
    _assert_identical(prefix.cache, ["aigateway_core.caching"])


def test_prefix_media_reexports():
    from aigateway_core import prefix
    from aigateway_core.media.plugin import MediaOptimizationPlugin

    assert prefix.media.plugin.MediaOptimizationPlugin is MediaOptimizationPlugin
    _assert_identical(prefix.media, ["aigateway_core.media"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_runtime_skeleton_prefix.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aigateway_core.prefix'`.

- [ ] **Step 3: Create `prefix/__init__.py`**

Create `aigateway-core/src/aigateway_core/prefix/__init__.py`:

```python
"""Shared pre-routing layer (总 1).

See ``docs/superpowers/specs/2026-07-07-runtime-structure-design.md``.
Everything under this package runs *before* dispatch splits the request
into an understanding or generation pipeline: PII, cache lookup/backfill,
media preprocessing.
"""
from aigateway_core.prefix import pii, cache, media  # noqa: F401

__all__ = ["pii", "cache", "media"]
```

- [ ] **Step 4: Create `prefix/pii/__init__.py`**

Create `aigateway-core/src/aigateway_core/prefix/pii/__init__.py`:

```python
"""PII detection / sanitize / reject — part of the shared prefix layer.

Authoritative implementations live in ``aigateway_core.security`` and
``aigateway_core.pipeline.PIIDetectorPlugin``.
"""
from aigateway_core import security as _security
from aigateway_core.pipeline import PIIDetectorPlugin

_public = [name for name in dir(_security) if not name.startswith("_")]
for _name in _public:
    globals()[_name] = getattr(_security, _name)

__all__ = _public + ["PIIDetectorPlugin"]
del _security, _public, _name
```

- [ ] **Step 5: Create `prefix/cache/__init__.py`**

Create `aigateway-core/src/aigateway_core/prefix/cache/__init__.py`:

```python
"""L1/L2/L3 cache orchestration — part of the shared prefix layer.

Authoritative implementations live in ``aigateway_core.caching`` and
``aigateway_core.pipeline.{PromptCachePlugin,SemanticCachePlugin}``.
"""
from aigateway_core import caching as _caching
from aigateway_core.pipeline import PromptCachePlugin, SemanticCachePlugin

_public = [name for name in dir(_caching) if not name.startswith("_")]
for _name in _public:
    globals()[_name] = getattr(_caching, _name)

__all__ = _public + ["PromptCachePlugin", "SemanticCachePlugin"]
del _caching, _public, _name
```

- [ ] **Step 6: Create `prefix/media/__init__.py`**

Create `aigateway-core/src/aigateway_core/prefix/media/__init__.py`:

```python
"""Media preprocessing (OCR / transcription / document parsing).

Part of the shared prefix layer. Authoritative implementation lives in
``aigateway_core.media`` and its subpackages.
"""
from aigateway_core import media as _media
from aigateway_core.media import plugin  # re-expose the plugin submodule

_public = [name for name in dir(_media) if not name.startswith("_")]
for _name in _public:
    globals()[_name] = getattr(_media, _name)

__all__ = _public + ["plugin"]
del _media, _public, _name
```

- [ ] **Step 7: Run smoke test**

Run: `python3 -m pytest tests/test_runtime_skeleton_prefix.py -v`
Expected: PASS.

- [ ] **Step 8: Run the full suite**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py`
Expected: same pass count as before Task 2.

- [ ] **Step 9: Commit**

```bash
git add aigateway-core/src/aigateway_core/prefix/ tests/test_runtime_skeleton_prefix.py
git commit -m "refactor: add aigateway_core.prefix (pii/cache/media) re-export layer"
```

---

## Task 3: `dispatch/` — total-orchestration layer

**Files:**
- Create: `aigateway-core/src/aigateway_core/dispatch/__init__.py`
- Create: `aigateway-core/src/aigateway_core/dispatch/context.py`
- Test: `tests/test_runtime_skeleton_dispatch.py`

**Interfaces:**
- Consumes: `aigateway_core.context.PipelineContext` and any other public names in `aigateway_core.context`.
- Produces: `aigateway_core.dispatch.context` re-exports every public name from `aigateway_core.context`. `aigateway_core.dispatch.PipelineContext` is a top-level alias.

**Note:** The full dispatcher/classifier implementation currently lives in `aigateway-api/src/aigateway_api/dispatcher.py` (see `docs/superpowers/specs/2026-07-07-runtime-structure-design.md` §Design Principles #3). Migrating the dispatcher out of the API surface is Phase 3 of the migration strategy and is **out of scope for this plan**. This task only stakes the core-side location for `PipelineContext`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_runtime_skeleton_dispatch.py`:

```python
"""Verify aigateway_core.dispatch re-exports PipelineContext."""
import importlib


def test_dispatch_context_reexports():
    dispatch_ctx = importlib.import_module("aigateway_core.dispatch.context")
    legacy_ctx = importlib.import_module("aigateway_core.context")
    for name in dir(legacy_ctx):
        if name.startswith("_"):
            continue
        assert hasattr(dispatch_ctx, name)
        assert getattr(dispatch_ctx, name) is getattr(legacy_ctx, name)


def test_dispatch_top_level_pipeline_context():
    from aigateway_core import dispatch
    from aigateway_core.context import PipelineContext

    assert dispatch.PipelineContext is PipelineContext
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_runtime_skeleton_dispatch.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aigateway_core.dispatch'`.

- [ ] **Step 3: Create `dispatch/__init__.py`**

Create `aigateway-core/src/aigateway_core/dispatch/__init__.py`:

```python
"""Dispatch layer (总 1 后半段).

The full dispatcher/classifier still lives in ``aigateway_api.dispatcher``
today; moving it into core is Phase 3 of the migration strategy in
``docs/superpowers/specs/2026-07-07-runtime-structure-design.md``.
This package currently exposes the shared ``PipelineContext`` under its
runtime-layer home.
"""
from aigateway_core.dispatch.context import PipelineContext  # noqa: F401

__all__ = ["PipelineContext"]
```

- [ ] **Step 4: Create `dispatch/context.py`**

Create `aigateway-core/src/aigateway_core/dispatch/context.py`:

```python
"""Backward-compat re-export of PipelineContext in the dispatch layer."""
from aigateway_core import context as _wrapped

_public = [name for name in dir(_wrapped) if not name.startswith("_")]
for _name in _public:
    globals()[_name] = getattr(_wrapped, _name)

__all__ = _public
del _wrapped, _public, _name
```

- [ ] **Step 5: Run smoke test**

Run: `python3 -m pytest tests/test_runtime_skeleton_dispatch.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py`
Expected: same pass count as before Task 3.

- [ ] **Step 7: Commit**

```bash
git add aigateway-core/src/aigateway_core/dispatch/ tests/test_runtime_skeleton_dispatch.py
git commit -m "refactor: add aigateway_core.dispatch re-export layer"
```

---

## Task 4: `pipelines/understanding/` — understanding pipeline skeleton

**Files:**
- Create: `aigateway-core/src/aigateway_core/pipelines/__init__.py`
- Create: `aigateway-core/src/aigateway_core/pipelines/understanding/__init__.py`
- Create: `aigateway-core/src/aigateway_core/pipelines/understanding/rag/__init__.py`
- Create: `aigateway-core/src/aigateway_core/pipelines/understanding/conversation/__init__.py`
- Create: `aigateway-core/src/aigateway_core/pipelines/understanding/compression/__init__.py`
- Test: `tests/test_runtime_skeleton_understanding.py`

**Interfaces:**
- Consumes: `aigateway_core.plugins.rag_retriever_plugin` (module), `aigateway_core.plugins.conv_compressor_plugin` (module), `aigateway_core.pipeline.PromptCompressPlugin`.
- Produces:
  - `aigateway_core.pipelines.understanding.rag` re-exports every public name from `aigateway_core.plugins.rag_retriever_plugin`.
  - `aigateway_core.pipelines.understanding.conversation` re-exports every public name from `aigateway_core.plugins.conv_compressor_plugin`.
  - `aigateway_core.pipelines.understanding.compression.PromptCompressPlugin` is the same object as `aigateway_core.pipeline.PromptCompressPlugin`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_runtime_skeleton_understanding.py`:

```python
"""Verify aigateway_core.pipelines.understanding re-exports."""
import importlib


def _assert_identical(new_mod, src_path):
    src_mod = importlib.import_module(src_path)
    for name in dir(src_mod):
        if name.startswith("_"):
            continue
        assert hasattr(new_mod, name), (
            f"{new_mod.__name__} missing {name!r} from {src_path}"
        )
        assert getattr(new_mod, name) is getattr(src_mod, name)


def test_understanding_rag_reexports():
    from aigateway_core.pipelines.understanding import rag
    _assert_identical(rag, "aigateway_core.plugins.rag_retriever_plugin")


def test_understanding_conversation_reexports():
    from aigateway_core.pipelines.understanding import conversation
    _assert_identical(conversation, "aigateway_core.plugins.conv_compressor_plugin")


def test_understanding_compression_reexports():
    from aigateway_core.pipelines.understanding import compression
    from aigateway_core.pipeline import PromptCompressPlugin

    assert compression.PromptCompressPlugin is PromptCompressPlugin
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_runtime_skeleton_understanding.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aigateway_core.pipelines'`.

- [ ] **Step 3: Create `pipelines/__init__.py`**

Create `aigateway-core/src/aigateway_core/pipelines/__init__.py`:

```python
"""Two main pipelines (分).

See ``docs/superpowers/specs/2026-07-07-runtime-structure-design.md``.
The understanding and generation pipelines are sibling subpackages; each
groups the runtime plugins that already exist elsewhere in the tree.
"""
from aigateway_core.pipelines import understanding, generation  # noqa: F401

__all__ = ["understanding", "generation"]
```

- [ ] **Step 4: Create `pipelines/understanding/__init__.py`**

Create `aigateway-core/src/aigateway_core/pipelines/understanding/__init__.py`:

```python
"""Understanding pipeline — optimizes inputs for understanding-oriented calls."""
from aigateway_core.pipelines.understanding import (  # noqa: F401
    rag,
    conversation,
    compression,
)

__all__ = ["rag", "conversation", "compression"]
```

- [ ] **Step 5: Create `pipelines/understanding/rag/__init__.py`**

Create `aigateway-core/src/aigateway_core/pipelines/understanding/rag/__init__.py`:

```python
"""RAG retrieval plugin — part of the understanding pipeline.

Authoritative implementation: ``aigateway_core.plugins.rag_retriever_plugin``.
"""
from aigateway_core.plugins import rag_retriever_plugin as _wrapped

_public = [name for name in dir(_wrapped) if not name.startswith("_")]
for _name in _public:
    globals()[_name] = getattr(_wrapped, _name)

__all__ = _public
del _wrapped, _public, _name
```

- [ ] **Step 6: Create `pipelines/understanding/conversation/__init__.py`**

Create `aigateway-core/src/aigateway_core/pipelines/understanding/conversation/__init__.py`:

```python
"""Conversation compression — part of the understanding pipeline.

Authoritative implementation: ``aigateway_core.plugins.conv_compressor_plugin``.
"""
from aigateway_core.plugins import conv_compressor_plugin as _wrapped

_public = [name for name in dir(_wrapped) if not name.startswith("_")]
for _name in _public:
    globals()[_name] = getattr(_wrapped, _name)

__all__ = _public
del _wrapped, _public, _name
```

- [ ] **Step 7: Create `pipelines/understanding/compression/__init__.py`**

Create `aigateway-core/src/aigateway_core/pipelines/understanding/compression/__init__.py`:

```python
"""Prompt token compression (LLMLingua-2) — part of the understanding pipeline.

Authoritative implementation: ``aigateway_core.pipeline.PromptCompressPlugin``.
"""
from aigateway_core.pipeline import PromptCompressPlugin

__all__ = ["PromptCompressPlugin"]
```

- [ ] **Step 8: Create the `generation/` stub so `pipelines/__init__.py` can import it**

The `pipelines/__init__.py` created in Step 3 imports both `understanding` and `generation`. Task 5 fills `generation/` in properly; this step creates the minimum stub so Task 4's tests run.

Create `aigateway-core/src/aigateway_core/pipelines/generation/__init__.py`:

```python
"""Generation pipeline — populated in the next task."""
__all__: list[str] = []
```

- [ ] **Step 9: Run smoke test**

Run: `python3 -m pytest tests/test_runtime_skeleton_understanding.py -v`
Expected: PASS.

- [ ] **Step 10: Run the full suite**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py`
Expected: same pass count as before Task 4.

- [ ] **Step 11: Commit**

```bash
git add aigateway-core/src/aigateway_core/pipelines/ tests/test_runtime_skeleton_understanding.py
git commit -m "refactor: add aigateway_core.pipelines.understanding re-export layer"
```

---

## Task 5: `pipelines/generation/` — generation pipeline skeleton

**Files:**
- Modify: `aigateway-core/src/aigateway_core/pipelines/generation/__init__.py` (replace the stub)
- Create: `aigateway-core/src/aigateway_core/pipelines/generation/director/__init__.py`
- Create: `aigateway-core/src/aigateway_core/pipelines/generation/intent/__init__.py`
- Create: `aigateway-core/src/aigateway_core/pipelines/generation/token/__init__.py`
- Create: `aigateway-core/src/aigateway_core/pipelines/generation/draft/__init__.py`
- Create: `aigateway-core/src/aigateway_core/pipelines/generation/cost/__init__.py`
- Create: `aigateway-core/src/aigateway_core/pipelines/generation/routing_signals/__init__.py`
- Test: `tests/test_runtime_skeleton_generation.py`

**Interfaces:**
- Consumes:
  - `aigateway_core.generation_optimization.strategies.ai_director` + `.plugins.ai_director_plugin`
  - `aigateway_core.generation_optimization.strategies.intent_evaluator` + `.plugins.intent_evaluator_plugin`
  - `aigateway_core.generation_optimization.strategies.token_compressor`, `.feature_cache`, `.prompt_confirmation`, `.prompt_template_manager`, `.video_preview`, and `.plugins.token_compressor_plugin`
  - `aigateway_core.generation_optimization.strategies.draft_generator` + `.plugins.draft_generator_plugin`
  - `aigateway_core.generation_optimization.plugins.cost_tracker_plugin`, `.metrics`, `.models`, `.api_key_groups`
  - `aigateway_core.generation_optimization.plugins.gen_model_router_plugin`
- Produces: one subpackage per bullet above under `aigateway_core.pipelines.generation.<name>`. Each subpackage re-exports every public name from every source module listed for it. Where a name collides across two source modules (e.g. `AIDirectorStrategy` if both defined it), the strategy module wins over the plugin module. There should be no collisions today, but the pattern below asserts that explicitly.

- [ ] **Step 1: Write the failing test**

Create `tests/test_runtime_skeleton_generation.py`:

```python
"""Verify aigateway_core.pipelines.generation re-exports."""
import importlib


SUBPACKAGES = {
    "director": [
        "aigateway_core.generation_optimization.strategies.ai_director",
        "aigateway_core.generation_optimization.plugins.ai_director_plugin",
    ],
    "intent": [
        "aigateway_core.generation_optimization.strategies.intent_evaluator",
        "aigateway_core.generation_optimization.plugins.intent_evaluator_plugin",
    ],
    "token": [
        "aigateway_core.generation_optimization.strategies.token_compressor",
        "aigateway_core.generation_optimization.strategies.feature_cache",
        "aigateway_core.generation_optimization.strategies.prompt_confirmation",
        "aigateway_core.generation_optimization.strategies.prompt_template_manager",
        "aigateway_core.generation_optimization.strategies.video_preview",
        "aigateway_core.generation_optimization.plugins.token_compressor_plugin",
    ],
    "draft": [
        "aigateway_core.generation_optimization.strategies.draft_generator",
        "aigateway_core.generation_optimization.plugins.draft_generator_plugin",
    ],
    "cost": [
        "aigateway_core.generation_optimization.plugins.cost_tracker_plugin",
        "aigateway_core.generation_optimization.metrics",
        "aigateway_core.generation_optimization.models",
        "aigateway_core.generation_optimization.api_key_groups",
    ],
    "routing_signals": [
        "aigateway_core.generation_optimization.plugins.gen_model_router_plugin",
    ],
}


def test_generation_subpackages_reexport_expected_sources():
    for subname, sources in SUBPACKAGES.items():
        sub = importlib.import_module(f"aigateway_core.pipelines.generation.{subname}")
        for src_path in sources:
            src_mod = importlib.import_module(src_path)
            for name in dir(src_mod):
                if name.startswith("_"):
                    continue
                assert hasattr(sub, name), (
                    f"aigateway_core.pipelines.generation.{subname} missing "
                    f"{name!r} from {src_path}"
                )
                # Strategy modules take precedence over plugin modules on collisions;
                # verify that the exported object is the same as *some* source's copy.
                assert any(
                    getattr(sub, name) is getattr(importlib.import_module(s), name, object())
                    for s in sources
                    if hasattr(importlib.import_module(s), name)
                ), f"aigateway_core.pipelines.generation.{subname}.{name} matches none of {sources}"


def test_generation_top_level_lists_all_subpackages():
    from aigateway_core.pipelines import generation

    for subname in SUBPACKAGES:
        assert subname in generation.__all__
        assert hasattr(generation, subname)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_runtime_skeleton_generation.py -v`
Expected: FAIL because the generation subpackages are empty stubs.

- [ ] **Step 3: Replace `pipelines/generation/__init__.py`**

Overwrite `aigateway-core/src/aigateway_core/pipelines/generation/__init__.py` with:

```python
"""Generation pipeline — optimizes generation requests for cost/success/fit.

Six functional groups matching the six existing generation plugins plus the
strategies each depends on. See
``docs/superpowers/specs/2026-07-07-runtime-structure-design.md``.
"""
from aigateway_core.pipelines.generation import (  # noqa: F401
    director,
    intent,
    token,
    draft,
    cost,
    routing_signals,
)

__all__ = ["director", "intent", "token", "draft", "cost", "routing_signals"]
```

- [ ] **Step 4: Create `pipelines/generation/director/__init__.py`**

```python
"""AI Director prompt shaping — part of the generation pipeline."""
from aigateway_core.generation_optimization.strategies import ai_director as _strategy
from aigateway_core.generation_optimization.plugins import ai_director_plugin as _plugin

_sources = (_strategy, _plugin)  # strategy first — its names win on collision
_names: list[str] = []
for _src in _sources:
    for _name in dir(_src):
        if _name.startswith("_"):
            continue
        if _name not in globals():
            globals()[_name] = getattr(_src, _name)
            _names.append(_name)

__all__ = _names
del _strategy, _plugin, _sources, _names, _src, _name
```

- [ ] **Step 5: Create `pipelines/generation/intent/__init__.py`**

```python
"""Intent / complexity evaluation — part of the generation pipeline."""
from aigateway_core.generation_optimization.strategies import intent_evaluator as _strategy
from aigateway_core.generation_optimization.plugins import intent_evaluator_plugin as _plugin

_sources = (_strategy, _plugin)
_names: list[str] = []
for _src in _sources:
    for _name in dir(_src):
        if _name.startswith("_"):
            continue
        if _name not in globals():
            globals()[_name] = getattr(_src, _name)
            _names.append(_name)

__all__ = _names
del _strategy, _plugin, _sources, _names, _src, _name
```

- [ ] **Step 6: Create `pipelines/generation/token/__init__.py`**

```python
"""Token / feature compression + template + preview — part of generation pipeline."""
from aigateway_core.generation_optimization.strategies import (
    token_compressor as _s_token,
    feature_cache as _s_fcache,
    prompt_confirmation as _s_confirm,
    prompt_template_manager as _s_tmpl,
    video_preview as _s_video,
)
from aigateway_core.generation_optimization.plugins import (
    token_compressor_plugin as _p_token,
)

_sources = (_s_token, _s_fcache, _s_confirm, _s_tmpl, _s_video, _p_token)
_names: list[str] = []
for _src in _sources:
    for _name in dir(_src):
        if _name.startswith("_"):
            continue
        if _name not in globals():
            globals()[_name] = getattr(_src, _name)
            _names.append(_name)

__all__ = _names
del _s_token, _s_fcache, _s_confirm, _s_tmpl, _s_video, _p_token
del _sources, _names, _src, _name
```

- [ ] **Step 7: Create `pipelines/generation/draft/__init__.py`**

```python
"""Draft-to-HiRes generation — part of the generation pipeline."""
from aigateway_core.generation_optimization.strategies import draft_generator as _strategy
from aigateway_core.generation_optimization.plugins import draft_generator_plugin as _plugin

_sources = (_strategy, _plugin)
_names: list[str] = []
for _src in _sources:
    for _name in dir(_src):
        if _name.startswith("_"):
            continue
        if _name not in globals():
            globals()[_name] = getattr(_src, _name)
            _names.append(_name)

__all__ = _names
del _strategy, _plugin, _sources, _names, _src, _name
```

- [ ] **Step 8: Create `pipelines/generation/cost/__init__.py`**

```python
"""Cost tracking + savings metrics + api-key groups — part of generation pipeline."""
from aigateway_core.generation_optimization import (
    metrics as _metrics,
    models as _models,
    api_key_groups as _keys,
)
from aigateway_core.generation_optimization.plugins import cost_tracker_plugin as _plugin

_sources = (_metrics, _models, _keys, _plugin)
_names: list[str] = []
for _src in _sources:
    for _name in dir(_src):
        if _name.startswith("_"):
            continue
        if _name not in globals():
            globals()[_name] = getattr(_src, _name)
            _names.append(_name)

__all__ = _names
del _metrics, _models, _keys, _plugin, _sources, _names, _src, _name
```

- [ ] **Step 9: Create `pipelines/generation/routing_signals/__init__.py`**

```python
"""Generation-side model routing signal — feeds the route/ layer.

The final model resolution decision lives in the ``route/`` layer; this
subpackage exposes the generation-specific plugin that emits the routing
signal.
"""
from aigateway_core.generation_optimization.plugins import gen_model_router_plugin as _wrapped

_public = [name for name in dir(_wrapped) if not name.startswith("_")]
for _name in _public:
    globals()[_name] = getattr(_wrapped, _name)

__all__ = _public
del _wrapped, _public, _name
```

- [ ] **Step 10: Run smoke test**

Run: `python3 -m pytest tests/test_runtime_skeleton_generation.py -v`
Expected: PASS.

- [ ] **Step 11: Run the full suite**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py`
Expected: same pass count as before Task 5.

- [ ] **Step 12: Commit**

```bash
git add aigateway-core/src/aigateway_core/pipelines/generation/ tests/test_runtime_skeleton_generation.py
git commit -m "refactor: add aigateway_core.pipelines.generation re-export layer"
```

---

## Task 6: `route/` — unified routing & response closure skeleton

**Files:**
- Create: `aigateway-core/src/aigateway_core/route/__init__.py`
- Create: `aigateway-core/src/aigateway_core/route/model_resolution/__init__.py`
- Create: `aigateway-core/src/aigateway_core/route/bridge/__init__.py`
- Test: `tests/test_runtime_skeleton_route.py`

**Interfaces:**
- Consumes: `aigateway_core.generation_optimization.strategies.model_router` (contains `ModelRouterStrategy`), `aigateway_core.litellm_bridge` (contains `LiteLLMBridge`, cooldown tracker, etc.).
- Produces:
  - `aigateway_core.route.model_resolution` re-exports every public name from `aigateway_core.generation_optimization.strategies.model_router`.
  - `aigateway_core.route.bridge` re-exports every public name from `aigateway_core.litellm_bridge`.
  - `aigateway_core.route.__all__` contains `["model_resolution", "bridge"]`.

**Note:** Streaming, response assembly, quota, and metrics closure currently live in the API surface (`aigateway_api.streaming`, `aigateway_api.openai_compat`, `aigateway_core.security`, `aigateway_core.metrics`). Migrating them into `route/` is Phase 3–4 of the design and is out of scope for this plan. This task stakes the core-side location for model resolution + bridge only.

- [ ] **Step 1: Write the failing test**

Create `tests/test_runtime_skeleton_route.py`:

```python
"""Verify aigateway_core.route re-exports."""
import importlib


def _assert_identical(new_mod, src_path):
    src_mod = importlib.import_module(src_path)
    for name in dir(src_mod):
        if name.startswith("_"):
            continue
        assert hasattr(new_mod, name), (
            f"{new_mod.__name__} missing {name!r} from {src_path}"
        )
        assert getattr(new_mod, name) is getattr(src_mod, name)


def test_route_model_resolution_reexports():
    from aigateway_core.route import model_resolution
    _assert_identical(
        model_resolution,
        "aigateway_core.generation_optimization.strategies.model_router",
    )


def test_route_bridge_reexports():
    from aigateway_core.route import bridge
    _assert_identical(bridge, "aigateway_core.litellm_bridge")


def test_route_all_lists_expected_subpackages():
    from aigateway_core import route

    assert sorted(route.__all__) == ["bridge", "model_resolution"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_runtime_skeleton_route.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aigateway_core.route'`.

- [ ] **Step 3: Create `route/__init__.py`**

```python
"""Unified routing + response closure (总 2).

See ``docs/superpowers/specs/2026-07-07-runtime-structure-design.md``.
This layer covers model resolution, LiteLLM bridge, provider fallback,
streaming/response assembly, and quota/metrics closure. This refactor
stakes the core-side location for model resolution and bridge only;
migrating streaming/response/quota out of the API surface is a later
phase.
"""
from aigateway_core.route import model_resolution, bridge  # noqa: F401

__all__ = ["model_resolution", "bridge"]
```

- [ ] **Step 4: Create `route/model_resolution/__init__.py`**

```python
"""Auto model resolution — part of the unified route layer.

Authoritative implementation:
``aigateway_core.generation_optimization.strategies.model_router``.
"""
from aigateway_core.generation_optimization.strategies import (
    model_router as _wrapped,
)

_public = [name for name in dir(_wrapped) if not name.startswith("_")]
for _name in _public:
    globals()[_name] = getattr(_wrapped, _name)

__all__ = _public
del _wrapped, _public, _name
```

- [ ] **Step 5: Create `route/bridge/__init__.py`**

```python
"""LiteLLM bridge — part of the unified route layer.

Authoritative implementation: ``aigateway_core.litellm_bridge``.
"""
from aigateway_core import litellm_bridge as _wrapped

_public = [name for name in dir(_wrapped) if not name.startswith("_")]
for _name in _public:
    globals()[_name] = getattr(_wrapped, _name)

__all__ = _public
del _wrapped, _public, _name
```

- [ ] **Step 6: Run smoke test**

Run: `python3 -m pytest tests/test_runtime_skeleton_route.py -v`
Expected: PASS.

- [ ] **Step 7: Run the full suite**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py`
Expected: same pass count as before Task 6.

- [ ] **Step 8: Commit**

```bash
git add aigateway-core/src/aigateway_core/route/ tests/test_runtime_skeleton_route.py
git commit -m "refactor: add aigateway_core.route (model_resolution/bridge) re-export layer"
```

---

## Task 7: Runtime map documentation + CLAUDE.md pointer

**Files:**
- Create: `docs/RUNTIME_MAP.md`
- Modify: `CLAUDE.md` (add one line under the "Key Files" table pointing to `docs/RUNTIME_MAP.md`)

**Interfaces:**
- Consumes: nothing (docs only).
- Produces: `docs/RUNTIME_MAP.md` — human-readable map from every existing module to its runtime layer, so future contributors can find things by layer or by legacy path.

- [ ] **Step 1: Write `docs/RUNTIME_MAP.md`**

Create `docs/RUNTIME_MAP.md`:

````markdown
# Runtime Layer Map

Cross-reference between legacy module paths and the 总分总 runtime layers
introduced by ``docs/superpowers/specs/2026-07-07-runtime-structure-design.md``.

Legacy paths keep working. New paths are canonical for new code.

## `shared/` — cross-layer utilities

| Legacy path                             | New path                                          |
|-----------------------------------------|---------------------------------------------------|
| `aigateway_core.config`                 | `aigateway_core.shared.config`                    |
| `aigateway_core.tracing`                | `aigateway_core.shared.tracing`                   |
| `aigateway_core.trace_event`            | `aigateway_core.shared.trace_event`               |
| `aigateway_core.exceptions`             | `aigateway_core.shared.exceptions`                |
| `aigateway_core.plugin_registry`        | `aigateway_core.shared.plugin_registry`           |
| `aigateway_core.logger`                 | `aigateway_core.shared.logger`                    |
| `aigateway_core.metrics`                | `aigateway_core.shared.metrics`                   |
| `aigateway_core.debug_config`           | `aigateway_core.shared.debug_config`              |
| `aigateway_core.redis_client`           | `aigateway_core.shared.redis_client`              |
| `aigateway_core.qdrant_client`          | `aigateway_core.shared.qdrant_client`             |
| `aigateway_core.integration_configs`    | `aigateway_core.shared.integration_configs`      |

## `prefix/` — shared pre-routing layer (总 1 前半段)

| Legacy path                                | New path                             |
|--------------------------------------------|--------------------------------------|
| `aigateway_core.security` (KeyStore, PII)  | `aigateway_core.prefix.pii`          |
| `aigateway_core.pipeline.PIIDetectorPlugin`| `aigateway_core.prefix.pii`          |
| `aigateway_core.caching`                   | `aigateway_core.prefix.cache`        |
| `aigateway_core.pipeline.PromptCachePlugin`| `aigateway_core.prefix.cache`        |
| `aigateway_core.pipeline.SemanticCachePlugin` | `aigateway_core.prefix.cache`     |
| `aigateway_core.media` + subpackages       | `aigateway_core.prefix.media`        |

## `dispatch/` — total orchestration (总 1 后半段)

| Legacy path                    | New path                              |
|--------------------------------|---------------------------------------|
| `aigateway_core.context`       | `aigateway_core.dispatch.context`     |

The full dispatcher/classifier still lives at ``aigateway_api.dispatcher``.
Migrating it into core is a later phase.

## `pipelines/understanding/` — understanding pipeline (分)

| Legacy path                                       | New path                                              |
|---------------------------------------------------|-------------------------------------------------------|
| `aigateway_core.plugins.rag_retriever_plugin`     | `aigateway_core.pipelines.understanding.rag`          |
| `aigateway_core.plugins.conv_compressor_plugin`   | `aigateway_core.pipelines.understanding.conversation` |
| `aigateway_core.pipeline.PromptCompressPlugin`    | `aigateway_core.pipelines.understanding.compression`  |

## `pipelines/generation/` — generation pipeline (分)

| Legacy path                                                                  | New path                                          |
|------------------------------------------------------------------------------|---------------------------------------------------|
| `aigateway_core.generation_optimization.strategies.ai_director`              | `aigateway_core.pipelines.generation.director`    |
| `aigateway_core.generation_optimization.plugins.ai_director_plugin`          | `aigateway_core.pipelines.generation.director`    |
| `aigateway_core.generation_optimization.strategies.intent_evaluator`         | `aigateway_core.pipelines.generation.intent`      |
| `aigateway_core.generation_optimization.plugins.intent_evaluator_plugin`     | `aigateway_core.pipelines.generation.intent`      |
| `aigateway_core.generation_optimization.strategies.token_compressor`         | `aigateway_core.pipelines.generation.token`       |
| `aigateway_core.generation_optimization.strategies.feature_cache`            | `aigateway_core.pipelines.generation.token`       |
| `aigateway_core.generation_optimization.strategies.prompt_confirmation`      | `aigateway_core.pipelines.generation.token`       |
| `aigateway_core.generation_optimization.strategies.prompt_template_manager`  | `aigateway_core.pipelines.generation.token`       |
| `aigateway_core.generation_optimization.strategies.video_preview`            | `aigateway_core.pipelines.generation.token`       |
| `aigateway_core.generation_optimization.plugins.token_compressor_plugin`     | `aigateway_core.pipelines.generation.token`       |
| `aigateway_core.generation_optimization.strategies.draft_generator`          | `aigateway_core.pipelines.generation.draft`       |
| `aigateway_core.generation_optimization.plugins.draft_generator_plugin`      | `aigateway_core.pipelines.generation.draft`       |
| `aigateway_core.generation_optimization.plugins.cost_tracker_plugin`         | `aigateway_core.pipelines.generation.cost`        |
| `aigateway_core.generation_optimization.metrics`                             | `aigateway_core.pipelines.generation.cost`        |
| `aigateway_core.generation_optimization.models`                              | `aigateway_core.pipelines.generation.cost`        |
| `aigateway_core.generation_optimization.api_key_groups`                      | `aigateway_core.pipelines.generation.cost`        |
| `aigateway_core.generation_optimization.plugins.gen_model_router_plugin`     | `aigateway_core.pipelines.generation.routing_signals` |

## `route/` — unified routing + response closure (总 2)

| Legacy path                                                          | New path                              |
|----------------------------------------------------------------------|---------------------------------------|
| `aigateway_core.generation_optimization.strategies.model_router`     | `aigateway_core.route.model_resolution` |
| `aigateway_core.litellm_bridge`                                      | `aigateway_core.route.bridge`         |

Streaming, response assembly, quota, and metrics closure remain in the API
surface for now. Moving them into `route/` is a later phase.

## Out of scope

`aigateway_core.code_rag.*`, `aigateway_core.pipeline.PipelineEngine`, and
the surface packages (`aigateway_api`, `aigateway_cli`, `control-panel`)
are not remapped by this refactor.
````

- [ ] **Step 2: Read the current CLAUDE.md Key Files table so the edit is anchored to real content**

Run: `grep -n "^| \`docs/DB_SCHEMA.md\`" CLAUDE.md`
Expected: one hit under the "Key Files" table.

- [ ] **Step 3: Add a Key Files entry pointing to the runtime map**

Edit `CLAUDE.md`. Under the "Key Files" table, immediately after the line
```
| `docs/DB_SCHEMA.md` | Redis keys, Qdrant collections, PipelineContext. |
```
insert:
```
| `docs/RUNTIME_MAP.md` | Legacy path → 总分总 runtime layer (prefix/dispatch/pipelines/route/shared). |
```

- [ ] **Step 4: Run the full suite one last time**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py`
Expected: same pass count as before Task 7 (docs-only change, so no test delta expected).

- [ ] **Step 5: Commit**

```bash
git add docs/RUNTIME_MAP.md CLAUDE.md
git commit -m "docs: add runtime layer map and CLAUDE.md pointer"
```

---

## Post-plan sanity check

After all 7 tasks land:

1. `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py` — full pass.
2. Spot-check one import path per layer from a Python REPL:
   ```python
   from aigateway_core.prefix.pii import PIIDetectorPlugin
   from aigateway_core.prefix.cache import CacheManager
   from aigateway_core.prefix.media import MediaCacheManager
   from aigateway_core.dispatch import PipelineContext
   from aigateway_core.pipelines.understanding.rag import RAGRetrieverPlugin
   from aigateway_core.pipelines.generation.director import AIDirectorPlugin
   from aigateway_core.route.model_resolution import ModelRouterStrategy
   from aigateway_core.route.bridge import LiteLLMBridge
   from aigateway_core.shared.config import ConfigManager
   ```
   All imports resolve without error.
3. `git log --oneline -10` shows 7 new commits with `refactor:`/`docs:` prefixes.

## Follow-up (out of scope for this plan)

The design spec lists Phase 3 (move dispatcher/orchestration out of `aigateway-api/openai_compat.py` and `aigateway-api/dispatcher.py` into core `dispatch/`) and Phase 4 (align runtime execution with runtime structure — e.g. surface no longer hand-walks the pipeline). Both should be their own specs+plans, driven by the new runtime map. Do not attempt them as a follow-up commit to this plan.
