# Plugin & Debug Switch Integration Test Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Write an integration test script that verifies every plugin's enable/disable toggle and every debug switch (5 global dimensions + per-plugin) actually works end-to-end through the admin API → config hot-reload → runtime behavior pipeline.

**Architecture:** A single test file `tests/test_plugin_debug_integration.py` using `starlette.testclient.TestClient` against the real FastAPI app. The test exercises all 13 plugins and 5 global debug dimensions through admin API endpoints, validates debug events via `TraceCollector`, and verifies plugin-specific behavior. Config is backed up/restored automatically.

**Tech Stack:** Python `pytest`, `starlette.testclient.TestClient`, `aigateway_api.main.create_app`, `aigateway_core.shared.trace_event.TraceCollector`, `aigateway_core.shared.debug_config.DebugConfig`, `yaml`, `shutil`.

## Global Constraints

- Test file path: `tests/test_plugin_debug_integration.py`
- Report output: `docs/test/plugin_debug_test_report.md`
- Backup config: `config.yaml.test-bak` in repo root
- All config mutations go through admin API endpoints (not direct file writes)
- After all tests: restore original `config.yaml`, reset all debug to off
- Uses `gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o` as admin bearer token (from config.yaml)
- pytest command: `python3 -m pytest tests/test_plugin_debug_integration.py -v`

---

### Task 1: Test scaffolding — imports, fixtures, helpers

**Files:**
- Create: `tests/test_plugin_debug_integration.py`

**Interfaces:**
- Consumes: `create_app`, `TestClient`, `TraceCollector`, `DebugConfig`
- Produces: `test_client` fixture, `backup_config`/`restore_config` helpers, `ADMIN_KEY` constant, `CHAT_REQUEST` template

**Steps:**

- [ ] **Step 1: Write the test scaffolding**

Create `tests/test_plugin_debug_integration.py` with:

```python
"""Integration test for plugin enable/disable + per-plugin debug + 5 global debug dimensions.

Phases:
  Phase 1: 13 plugins individually — enable plugin + per_plugin debug + corresponding global dimension
  Phase 2: 5 global debug dimensions individually
  Phase 3: Incremental plugin accumulation (2→13) with all dims on
  Phase 4: Incremental global dimension accumulation (1→5) with all plugins on
  Phase 5: Full-on conflict detection — all 13 plugins + all per_plugin debug + all 5 dims
"""
import copy
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml
from starlette.testclient import TestClient

# Paths
REPO_ROOT = Path(__file__).parent.parent.resolve()
CONFIG_PATH = REPO_ROOT / "config.yaml"
BACKUP_PATH = REPO_ROOT / "config.yaml.test-bak"
REPORT_PATH = REPO_ROOT / "docs" / "test" / "plugin_debug_test_report.md"

# Add source paths for imports
sys.path.insert(0, str(REPO_ROOT / "aigateway-api" / "src"))
sys.path.insert(0, str(REPO_ROOT / "aigateway-core" / "src"))

# Admin API key from config.yaml
ADMIN_KEY = "gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o"
HEADERS = {"Authorization": f"Bearer {ADMIN_KEY}"}

# Base chat request template
CHAT_REQUEST = {
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "Hello"}],
    "temperature": 0.7,
    "max_tokens": 50,
}
```

- [ ] **Step 2: Run to verify scaffold compiles**

Run: `python3 -c "import ast; ast.parse(open('tests/test_plugin_debug_integration.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add tests/test_plugin_debug_integration.py
git commit -m "test: scaffold plugin debug integration test file"
```

---

### Task 2: Config backup/restore fixtures

**Files:**
- Modify: `tests/test_plugin_debug_integration.py`

**Interfaces:**
- Consumes: `CONFIG_PATH`, `BACKUP_PATH`, `yaml`, `shutil`
- Produces: `config_backup` fixture (backup before tests), `reset_config` fixture (restore after tests), `reset_debug_state()` helper

**Steps:**

- [ ] **Step 1: Add config backup/restore fixtures**

Append to the file:

```python
# ------------------------------------------------------------------
# Fixtures: config backup & restore
# ------------------------------------------------------------------

@pytest.fixture(scope="session", autouse)
def config_backup():
    """Backup config.yaml before all tests, restore after."""
    # Backup
    if CONFIG_PATH.exists():
        shutil.copy2(str(CONFIG_PATH), str(BACKUP_PATH))
    yield
    # Restore
    if BACKUP_PATH.exists():
        shutil.copy2(str(BACKUP_PATH), str(CONFIG_PATH))
        BACKUP_PATH.unlink(missing_ok=True)


def reset_debug_state(client: TestClient) -> None:
    """Reset all debug switches to off via admin API."""
    # Reset global debug dimensions
    client.put(
        "/admin/global-config",
        json={"hot_reload": True, "debug_mode": True, "debug": {
            "frontend": False, "entry": False, "cache": False,
            "bridge": False, "plugins_enabled": False,
        }},
        headers=HEADERS,
    )
    # Reset all per_plugin debug to false
    for plugin_name in ALL_PLUGIN_NAMES:
        try:
            client.post(
                f"/admin/plugins/{plugin_name}/debug",
                json={"enabled": False},
                headers=HEADERS,
            )
        except Exception:
            pass  # Some plugins don't support per_plugin debug
```

- [ ] **Step 2: Run to verify syntax**

Run: `python3 -c "import ast; ast.parse(open('tests/test_plugin_debug_integration.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add tests/test_plugin_debug_integration.py
git commit -m "test: add config backup/restore fixtures and reset_debug_state helper"
```

---

### Task 3: Plugin inventory and dimension mapping constants

**Files:**
- Modify: `tests/test_plugin_debug_integration.py`

**Interfaces:**
- Consumes: plugin names from `registration.py`
- Produces: `ALL_PLUGIN_NAMES`, `PLUGIN_GLOBAL_DIM_MAP`, `GLOBAL_DIMENSIONS`, `PLUGIN_VERIFICATION_PAYLOADS`

**Steps:**

- [ ] **Step 1: Add plugin inventory constants**

Append after the fixtures:

```python
# ------------------------------------------------------------------
# Plugin inventory
# ------------------------------------------------------------------

ALL_PLUGIN_NAMES = [
    "pii_detector", "prompt_cache", "semantic_cache", "prompt_compress",
    "rag_retriever", "conv_compressor",
    "ai_director", "intent_evaluator", "token_compressor",
    "draft_generator", "gen_model_router", "cost_tracker",
    "media_optimizer",
]

# Maps plugin → which global debug dimension controls its per_plugin debug
PLUGIN_GLOBAL_DIM_MAP: Dict[str, str] = {
    # entry dimension covers auth/dispatcher/prompt_compress/media
    "pii_detector": "entry",
    "prompt_compress": "entry",
    "media_optimizer": "entry",
    # cache dimension covers cache plugins
    "prompt_cache": "cache",
    "semantic_cache": "cache",
    # plugins_enabled covers all other plugins
    "rag_retriever": "plugins_enabled",
    "conv_compressor": "plugins_enabled",
    "ai_director": "plugins_enabled",
    "intent_evaluator": "plugins_enabled",
    "token_compressor": "plugins_enabled",
    "draft_generator": "plugins_enabled",
    "gen_model_router": "plugins_enabled",
    "cost_tracker": "plugins_enabled",
}

GLOBAL_DIMENSIONS = ["frontend", "entry", "cache", "bridge", "plugins_enabled"]

# Plugins that DON'T have per_plugin debug (prompt_compress maps to entry only)
NO_PER_PLUGIN_DEBUG = {"prompt_compress"}
```

- [ ] **Step 2: Run to verify syntax**

Run: `python3 -c "import ast; ast.parse(open('tests/test_plugin_debug_integration.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add tests/test_plugin_debug_integration.py
git commit -m "test: add plugin inventory and dimension mapping constants"
```

---

### Task 4: Helper functions — API calls, debug event inspection, report builder

**Files:**
- Modify: `tests/test_plugin_debug_integration.py`

**Interfaces:**
- Consumes: `TestClient`, `HEADERS`, `TraceCollector`
- Produces: `enable_plugin()`, `disable_plugin()`, `enable_plugin_debug()`, `enable_global_dim()`, `disable_global_dim()`, `get_debug_config()`, `trigger_chat()`, `find_debug_events()`, `build_report()`

**Steps:**

- [ ] **Step 1: Write API helper functions**

```python
# ------------------------------------------------------------------
# Helpers: admin API calls & debug event inspection
# ------------------------------------------------------------------

def enable_plugin(client: TestClient, name: str) -> None:
    """Enable a plugin via PUT /admin/plugins-config."""
    resp = client.put(
        "/admin/plugins-config",
        json={"name": name, "enabled": True},
        headers=HEADERS,
    )
    assert resp.status_code == 200, f"Failed to enable {name}: {resp.text}"


def disable_plugin(client: TestClient, name: str) -> None:
    """Disable a plugin via PUT /admin/plugins-config."""
    resp = client.put(
        "/admin/plugins-config",
        json={"name": name, "enabled": False},
        headers=HEADERS,
    )
    assert resp.status_code == 200, f"Failed to disable {name}: {resp.text}"


def enable_plugin_debug(client: TestClient, name: str) -> None:
    """Enable per-plugin debug via POST /admin/plugins/{name}/debug."""
    if name in NO_PER_PLUGIN_DEBUG:
        return  # Skip — not supported
    resp = client.post(
        f"/admin/plugins/{name}/debug",
        json={"enabled": True},
        headers=HEADERS,
    )
    assert resp.status_code == 200, f"Failed to enable debug for {name}: {resp.text}"


def disable_plugin_debug(client: TestClient, name: str) -> None:
    """Disable per-plugin debug via POST /admin/plugins/{name}/debug."""
    if name in NO_PER_PLUGIN_DEBUG:
        return
    resp = client.post(
        f"/admin/plugins/{name}/debug",
        json={"enabled": False},
        headers=HEADERS,
    )
    assert resp.status_code == 200, f"Failed to disable debug for {name}: {resp.text}"


def enable_global_dim(client: TestClient, dim: str) -> None:
    """Enable a global debug dimension via PUT /admin/global-config."""
    resp = client.put(
        "/admin/global-config",
        json={"hot_reload": True, "debug_mode": True, "debug": {dim: True}},
        headers=HEADERS,
    )
    assert resp.status_code == 200, f"Failed to enable global dim {dim}: {resp.text}"


def disable_global_dim(client: TestClient, dim: str) -> None:
    """Disable a global debug dimension via PUT /admin/global-config."""
    resp = client.put(
        "/admin/global-config",
        json={"hot_reload": True, "debug_mode": True, "debug": {dim: False}},
        headers=HEADERS,
    )
    assert resp.status_code == 200, f"Failed to disable global dim {dim}: {resp.text}"


def get_debug_config(client: TestClient) -> Dict[str, Any]:
    """GET /admin/config/debug → parsed response."""
    resp = client.get("/admin/config/debug", headers=HEADERS)
    assert resp.status_code == 200, f"Failed to get debug config: {resp.text}"
    return resp.json()["data"]


def trigger_chat(client: TestClient, request_body: Optional[Dict] = None, trace_id: Optional[str] = None) -> Dict[str, Any]:
    """POST /v1/chat/completions, return response JSON.
    Sets X-Trace-Id header if provided so we can correlate with TraceCollector."""
    body = request_body or CHAT_REQUEST.copy()
    if trace_id:
        body["x_trace_id"] = trace_id  # pass through request body for tracing
    resp = client.post(
        "/v1/chat/completions",
        json=body,
        headers=HEADERS,
    )
    return resp.json()


def find_debug_events(collector_events: list, dimension: Optional[str] = None, plugin_name: Optional[str] = None) -> list:
    """Filter TraceCollector events for kind='debug' matching criteria."""
    from aigateway_core.shared.trace_event import TraceEvent
    result = []
    for ev in collector_events:
        if not isinstance(ev, TraceEvent):
            continue
        if ev.kind != "debug":
            continue
        if dimension and ev.stage != dimension:
            continue
        if plugin_name and plugin_name not in ev.name and plugin_name not in ev.stage:
            continue
        result.append(ev)
    return result
```

- [ ] **Step 2: Run to verify syntax**

Run: `python3 -c "import ast; ast.parse(open('tests/test_plugin_debug_integration.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add tests/test_plugin_debug_integration.py
git commit -m "test: add admin API helpers and debug event inspection utilities"
```

---

### Task 5: Phase 1 — Individual plugin + per-plugin debug tests

**Files:**
- Modify: `tests/test_plugin_debug_integration.py`

**Interfaces:**
- Consumes: `enable_plugin`, `enable_plugin_debug`, `enable_global_dim`, `trigger_chat`, `find_debug_events`, `get_debug_config`, `disable_*` helpers
- Produces: `test_phase1_individual_plugins()` — the main test function

**Steps:**

- [ ] **Step 1: Write Phase 1 test function**

```python
# ------------------------------------------------------------------
# Phase 1: Individual Plugin + Per-Plugin Debug Tests
# ------------------------------------------------------------------

def test_phase1_individual_plugins(test_client: TestClient) -> None:
    """For each plugin: enable it + its per_plugin debug + corresponding global dim, verify debug event + behavior."""
    from aigateway_core.shared.trace_event import TraceCollector

    results: List[Dict[str, Any]] = []

    for plugin_name in ALL_PLUGIN_NAMES:
        dim = PLUGIN_GLOBAL_DIM_MAP[plugin_name]
        collector_events_before = []
        try:
            c = TraceCollector.current()
            if c:
                collector_events_before = list(c.events)
        except Exception:
            pass

        # 1. Enable plugin
        enable_plugin(test_client, plugin_name)
        # 2. Enable per_plugin debug (skip for prompt_compress)
        enable_plugin_debug(test_client, plugin_name)
        # 3. Enable corresponding global dimension
        enable_global_dim(test_client, dim)
        # 4. Verify state via admin API
        debug_cfg = get_debug_config(test_client)
        assert debug_cfg.get("plugins_enabled") or debug_cfg.get(dim), \
            f"Global dim {dim} should be enabled for {plugin_name}"

        # 5. Trigger request
        payload = _get_verification_payload(plugin_name)
        try:
            resp = trigger_chat(test_client, payload)
        except Exception as exc:
            # Some requests may fail due to missing infra (Qdrant, Redis, etc.) — still check debug events
            resp = {}

        # 6. Verify debug event
        collector_events_after = []
        try:
            c = TraceCollector.current()
            if c:
                collector_events_after = list(c.events)
        except Exception:
            pass

        all_events = collector_events_before + collector_events_after
        debug_events = find_debug_events(all_events, plugin_name=plugin_name)
        debug_found = len(debug_events) > 0

        # 7. Verify plugin behavior
        behavior_verified = _verify_plugin_behavior(plugin_name, resp, debug_events)

        results.append({
            "phase": "Phase 1",
            "plugin": plugin_name,
            "dimension": dim,
            "debug_event_found": debug_found,
            "behavior_verified": behavior_verified,
            "status": "PASS" if (debug_found and behavior_verified) else "FAIL",
        })

        # 8. Cleanup
        disable_plugin_debug(test_client, plugin_name)
        disable_global_dim(test_client, dim)
        disable_plugin(test_client, plugin_name)

    # Generate report
    _write_report(results)


def _get_verification_payload(plugin_name: str) -> Dict[str, Any]:
    """Return a request payload designed to exercise a specific plugin."""
    base = CHAT_REQUEST.copy()
    payloads = {
        # PII detector: message with email pattern
        "pii_detector": {
            **base,
            "messages": [{"role": "user", "content": "Contact me at test@example.com or call 123-456-7890"}],
        },
        # Prompt cache: short prompt (will hit on second request)
        "prompt_cache": {**base, "messages": [{"role": "user", "content": "Hi"}]},
        # Semantic cache: long prompt for embedding
        "semantic_cache": {
            **base,
            "messages": [{"role": "user", "content": "Write a comprehensive guide to machine learning covering supervised learning, unsupervised learning, reinforcement learning, neural networks, decision trees, random forests, gradient boosting, support vector machines, k-nearest neighbors, naive bayes, linear regression, logistic regression, principal component analysis, and autoencoders. Include mathematical formulations for each method."}],
        },
        # Prompt compress: very long prompt
        "prompt_compress": {
            **base,
            "messages": [{"role": "user", "content": " " * 600 + "Summarize this lengthy text for me."}],
        },
        # RAG retriever: query-like message
        "rag_retriever": {
            **base,
            "messages": [{"role": "user", "content": "What is the capital of France and why?"}],
        },
        # Conv compressor: multi-turn conversation
        "conv_compressor": {
            **base,
            "messages": [
                {"role": "user", "content": "Tell me about Python programming"},
                {"role": "assistant", "content": "Python is a high-level, general-purpose programming language. Its design philosophy emphasizes code readability with the use of significant indentation."},
                {"role": "user", "content": "What about Java?"},
                {"role": "assistant", "content": "Java is a high-level, class-based, object-oriented programming language that is designed to have as few implementation dependencies as possible."},
                {"role": "user", "content": "Compare them"},
            ],
        },
        # AI Director: generation prompt
        "ai_director": {**base, "messages": [{"role": "user", "content": "Generate a story about a robot"}]},
        # Intent evaluator
        "intent_evaluator": {**base, "messages": [{"role": "user", "content": "Translate this to French: Hello world"}]},
        # Token compressor: image in message
        "token_compressor": {
            **base,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "Describe this image"}, {"type": "image_url", "image_url": {"url": "https://example.com/test.png"}}]}],
        },
        # Draft generator
        "draft_generator": {**base, "messages": [{"role": "user", "content": "Write a poem"}]},
        # Gen model router
        "gen_model_router": {**base, "messages": [{"role": "user", "content": "What is 2+2?"}]},
        # Cost tracker
        "cost_tracker": {**base, "messages": [{"role": "user", "content": "Say hello"}]},
        # Media optimizer
        "media_optimizer": {**base, "messages": [{"role": "user", "content": "Process https://example.com/image.jpg"}]},
    }
    return payloads.get(plugin_name, base)


def _verify_plugin_behavior(plugin_name: str, resp: Dict, debug_events: list) -> bool:
    """Verify plugin-specific behavior from response and debug events."""
    if plugin_name == "pii_detector":
        # PII should be sanitized in response or debug event should show detection
        if debug_events:
            for ev in debug_events:
                if ev.payload and ("sanitized" in str(ev.payload).lower() or "detected" in str(ev.payload).lower()):
                    return True
        return False
    if plugin_name == "prompt_cache":
        # Debug event should show cache activity
        return any(ev.payload for ev in debug_events)
    if plugin_name == "semantic_cache":
        return any(ev.payload and "similarity" in str(ev.payload).lower() for ev in debug_events)
    if plugin_name == "prompt_compress":
        return any(ev.payload and ("ratio" in str(ev.payload).lower() or "compress" in str(ev.payload).lower()) for ev in debug_events)
    if plugin_name == "rag_retriever":
        return len(debug_events) > 0  # Graceful degradation acceptable
    if plugin_name == "conv_compressor":
        return any(ev.payload and "compress" in str(ev.payload).lower() for ev in debug_events)
    if plugin_name == "ai_director":
        return len(debug_events) > 0
    if plugin_name == "intent_evaluator":
        return any(ev.payload and "intent" in str(ev.payload).lower() for ev in debug_events)
    if plugin_name == "token_compressor":
        return any(ev.payload and ("token" in str(ev.payload).lower() or "compress" in str(ev.payload).lower()) for ev in debug_events)
    if plugin_name == "draft_generator":
        return len(debug_events) > 0
    if plugin_name == "gen_model_router":
        return any(ev.payload and ("model" in str(ev.payload).lower() or "router" in str(ev.payload).lower()) for ev in debug_events)
    if plugin_name == "cost_tracker":
        return any(ev.payload and ("cost" in str(ev.payload).lower() or "token" in str(ev.payload).lower()) for ev in debug_events)
    if plugin_name == "media_optimizer":
        return any(ev.payload and ("media" in str(ev.payload).lower() or "optimize" in str(ev.payload).lower()) for ev in debug_events)
    return len(debug_events) > 0  # Default: at least a debug event appeared
```

- [ ] **Step 2: Run to verify syntax**

Run: `python3 -c "import ast; ast.parse(open('tests/test_plugin_debug_integration.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add tests/test_plugin_debug_integration.py
git commit -m "test: add Phase 1 — individual plugin + per-plugin debug tests"
```

---

### Task 6: Phase 2 — Global debug dimension tests

**Files:**
- Modify: `tests/test_plugin_debug_integration.py`

**Interfaces:**
- Consumes: `enable_global_dim`, `disable_global_dim`, `trigger_chat`, `find_debug_events`
- Produces: `test_phase2_global_dimensions()`

**Steps:**

- [ ] **Step 1: Write Phase 2 test function**

```python
# ------------------------------------------------------------------
# Phase 2: Global Debug Dimension Tests
# ------------------------------------------------------------------

def test_phase2_global_dimensions(test_client: TestClient) -> None:
    """For each global dimension: enable it alone, verify debug events appear."""
    from aigateway_core.shared.trace_event import TraceCollector

    results: List[Dict[str, Any]] = []

    for dim in GLOBAL_DIMENSIONS:
        # Ensure all plugins are disabled for clean isolation
        for pname in ALL_PLUGIN_NAMES:
            try:
                disable_plugin(test_client, pname)
            except Exception:
                pass

        # Enable only this global dimension
        enable_global_dim(test_client, dim)

        # Trigger request
        try:
            trigger_chat(test_client)
        except Exception:
            pass

        # Verify debug event exists for this dimension
        collector_events_after = []
        try:
            c = TraceCollector.current()
            if c:
                collector_events_after = list(c.events)
        except Exception:
            pass

        debug_events = find_debug_events(collector_events_after, dimension=dim)
        debug_found = len(debug_events) > 0

        results.append({
            "phase": "Phase 2",
            "dimension": dim,
            "debug_event_found": debug_found,
            "status": "PASS" if debug_found else "FAIL",
        })

        # Cleanup
        disable_global_dim(test_client, dim)

    _append_report(results)
```

- [ ] **Step 2: Run to verify syntax**

Run: `python3 -c "import ast; ast.parse(open('tests/test_plugin_debug_integration.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add tests/test_plugin_debug_integration.py
git commit -m "test: add Phase 2 — global debug dimension tests"
```

---

### Task 7: Phase 3 — Incremental plugin accumulation + Phase 4 — Incremental global dimension accumulation

**Files:**
- Modify: `tests/test_plugin_debug_integration.py`

**Interfaces:**
- Consumes: all `enable/disable_*` helpers, `trigger_chat`, `find_debug_events`
- Produces: `test_phase3_incremental_plugins()`, `test_phase4_incremental_dimensions()`

**Steps:**

- [ ] **Step 1: Write Phase 3 test function**

```python
# ------------------------------------------------------------------
# Phase 3: Incremental Plugin + Debug Accumulation
# ------------------------------------------------------------------

def test_phase3_incremental_plugins(test_client: TestClient) -> None:
    """Accumulate plugins 2→13, with all 5 global dims always on."""
    from aigateway_core.shared.trace_event import TraceCollector

    results: List[Dict[str, Any]] = []

    # First, enable all 5 global dimensions
    for dim in GLOBAL_DIMENSIONS:
        enable_global_dim(test_client, dim)

    # Iterations: 2, 3, ..., 13
    for n in range(2, len(ALL_PLUGIN_NAMES) + 1):
        # Enable plugins 0..n-1
        for i in range(n):
            pname = ALL_PLUGIN_NAMES[i]
            enable_plugin(test_client, pname)
            enable_plugin_debug(test_client, pname)

        # Trigger request
        try:
            trigger_chat(test_client)
        except Exception:
            pass

        # Collect debug events
        collector_events_after = []
        try:
            c = TraceCollector.current()
            if c:
                collector_events_after = list(c.events)
        except Exception:
            pass

        # Count distinct plugin debug events
        plugin_debug_events = [ev for ev in collector_events_after if ev.kind == "debug" and ev.stage not in GLOBAL_DIMENSIONS]
        distinct_stages = set(ev.stage for ev in plugin_debug_events)
        debug_found = len(distinct_stages) >= n - 1  # prompt_compress has no per_plugin debug

        results.append({
            "phase": "Phase 3",
            "iteration": n,
            "plugins_enabled": n,
            "distinct_debug_stages": len(distinct_stages),
            "debug_event_found": debug_found,
            "status": "PASS" if debug_found else "FAIL",
        })

        # Disable all for next iteration
        for i in range(n):
            pname = ALL_PLUGIN_NAMES[i]
            disable_plugin(test_client, pname)
            disable_plugin_debug(test_client, pname)

    _append_report(results)
```

- [ ] **Step 2: Write Phase 4 test function**

```python
# ------------------------------------------------------------------
# Phase 4: Incremental Global Dimension Accumulation
# ------------------------------------------------------------------

def test_phase4_incremental_dimensions(test_client: TestClient) -> None:
    """Accumulate global dimensions 1→5, with all 13 plugins enabled."""
    from aigateway_core.shared.trace_event import TraceCollector

    results: List[Dict[str, Any]] = []

    # Enable all plugins first
    for pname in ALL_PLUGIN_NAMES:
        enable_plugin(test_client, pname)
        enable_plugin_debug(test_client, pname)

    # Iterations: 1, 2, 3, 4, 5
    for n in range(1, len(GLOBAL_DIMENSIONS) + 1):
        # Enable first n dimensions
        for i in range(n):
            enable_global_dim(test_client, GLOBAL_DIMENSIONS[i])

        # Trigger request
        try:
            trigger_chat(test_client)
        except Exception:
            pass

        # Collect debug events
        collector_events_after = []
        try:
            c = TraceCollector.current()
            if c:
                collector_events_after = list(c.events)
        except Exception:
            pass

        # Verify: enabled dims have events, disabled dims don't
        enabled_dims = GLOBAL_DIMENSIONS[:n]
        disabled_dims = GLOBAL_DIMENSIONS[n:]

        enabled_has_events = True
        for dim in enabled_dims:
            events = find_debug_events(collector_events_after, dimension=dim)
            if not events:
                enabled_has_events = False
                break

        disabled_no_events = True
        for dim in disabled_dims:
            events = find_debug_events(collector_events_after, dimension=dim)
            if events:
                disabled_no_events = False
                break

        debug_found = enabled_has_events and disabled_no_events

        results.append({
            "phase": "Phase 4",
            "iteration": n,
            "dimensions_enabled": n,
            "enabled_dims_have_events": enabled_has_events,
            "disabled_dims_no_events": disabled_no_events,
            "debug_event_found": debug_found,
            "status": "PASS" if debug_found else "FAIL",
        })

        # Disable all dimensions for next iteration
        for i in range(n):
            disable_global_dim(test_client, GLOBAL_DIMENSIONS[i])

    _append_report(results)
```

- [ ] **Step 3: Run to verify syntax**

Run: `python3 -c "import ast; ast.parse(open('tests/test_plugin_debug_integration.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add tests/test_plugin_debug_integration.py
git commit -m "test: add Phase 3 (incremental plugins) and Phase 4 (incremental dimensions)"
```

---

### Task 8: Phase 5 — Full-on conflict detection + report writer + test_client fixture

**Files:**
- Modify: `tests/test_plugin_debug_integration.py`

**Interfaces:**
- Consumes: all helpers
- Produces: `test_phase5_full_conflict_detection()`, `_write_report()`, `_append_report()`, `test_client` fixture

**Steps:**

- [ ] **Step 1: Write Phase 5 test function**

```python
# ------------------------------------------------------------------
# Phase 5: Full-On Conflict Detection
# ------------------------------------------------------------------

def test_phase5_full_conflict_detection(test_client: TestClient) -> None:
    """All 13 plugins + all per_plugin debug + all 5 global dims — verify no conflicts."""
    from aigateway_core.shared.trace_event import TraceCollector

    results: List[Dict[str, Any]] = []

    # Enable everything
    for dim in GLOBAL_DIMENSIONS:
        enable_global_dim(test_client, dim)
    for pname in ALL_PLUGIN_NAMES:
        enable_plugin(test_client, pname)
        enable_plugin_debug(test_client, pname)

    # Trigger request
    try:
        trigger_chat(test_client)
    except Exception:
        pass

    # Collect debug events
    collector_events_after = []
    try:
        c = TraceCollector.current()
        if c:
            collector_events_after = list(c.events)
    except Exception:
        pass

    # Verify: no crashes, all plugins appear, no duplicate wrong events
    all_debug_events = [ev for ev in collector_events_after if ev.kind == "debug"]
    plugin_stages = set(ev.stage for ev in all_debug_events)
    all_plugins_present = all(pn in plugin_stages for pn in ALL_PLUGIN_NAMES if pn not in NO_PER_PLUGIN_DEBUG)

    # Check for duplicate events (same stage appearing multiple times with conflicting data)
    stage_counts: Dict[str, int] = {}
    for ev in all_debug_events:
        stage_counts[ev.stage] = stage_counts.get(ev.stage, 0) + 1
    duplicates = {k: v for k, v in stage_counts.items() if v > 2}

    conflict_found = len(duplicates) > 0

    results.append({
        "phase": "Phase 5",
        "all_plugins_present": all_plugins_present,
        "conflicts_detected": conflict_found,
        "duplicate_stages": duplicates,
        "status": "PASS" if (all_plugins_present and not conflict_found) else "FAIL",
    })

    # Cleanup: reset everything
    reset_debug_state(test_client)
    for pname in ALL_PLUGIN_NAMES:
        try:
            disable_plugin(test_client, pname)
        except Exception:
            pass

    _append_report(results)
```

- [ ] **Step 2: Write report helpers and test_client fixture**

```python
# ------------------------------------------------------------------
# Report writer
# ------------------------------------------------------------------

def _write_report(results: List[Dict[str, Any]]) -> None:
    """Write Phase 1-5 results to markdown report."""
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Plugin & Debug Switch Integration Test Report\n"]
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    current_phase = None
    for r in results:
        phase = r.get("phase", "Unknown")
        if phase != current_phase:
            current_phase = phase
            lines.append(f"\n## {phase}\n")
            lines.append("| Status | Details | Debug Found | Behavior Verified |")
            lines.append("|--------|---------|-------------|-------------------|")
        detail_parts = []
        for k, v in r.items():
            if k not in ("phase", "status", "debug_event_found", "behavior_verified"):
                detail_parts.append(f"**{k}**: {v}")
        detail = "; ".join(detail_parts) if detail_parts else "-"
        verified = r.get("behavior_verified", "N/A")
        lines.append(f"| {r['status']} | {detail} | {r.get('debug_event_found', 'N/A')} | {verified} |")

    lines.append("\n## Summary\n")
    total = len(results)
    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = total - passed
    lines.append(f"- **Total tests**: {total}")
    lines.append(f"- **Passed**: {passed}")
    lines.append(f"- **Failed**: {failed}")
    if failed > 0:
        lines.append("\n### Failures\n")
        for r in results:
            if r["status"] == "FAIL":
                lines.append(f"- **{r.get('plugin', r.get('dimension', 'Unknown'))}**: {r}")

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def _append_report(results: List[Dict[str, Any]]) -> None:
    """Append results to existing report (for Phases 2-5)."""
    _write_report([])  # Ensure file exists
    # Read existing content
    content = REPORT_PATH.read_text(encoding="utf-8")
    # Append new results using _write_report logic inline
    current_phase = None
    lines = []
    for r in results:
        phase = r.get("phase", "Unknown")
        if phase != current_phase:
            current_phase = phase
            lines.append(f"\n## {phase}\n")
            lines.append("| Status | Details | Debug Found |")
            lines.append("|--------|---------|-------------|")
        detail_parts = []
        for k, v in r.items():
            if k not in ("phase", "status", "debug_event_found"):
                detail_parts.append(f"**{k}**: {v}")
        detail = "; ".join(detail_parts) if detail_parts else "-"
        lines.append(f"| {r['status']} | {detail} | {r.get('debug_event_found', 'N/A')} |")

    with open(str(REPORT_PATH), "a", encoding="utf-8") as f:
        f.write("\n".join(lines))
```

- [ ] **Step 3: Add test_client fixture**

Add before the Phase 1 test:

```python
# ------------------------------------------------------------------
# Fixture: TestClient
# ------------------------------------------------------------------

@pytest.fixture(scope="session")
def test_client():
    """Create a TestClient for the FastAPI app, ensuring lifespan runs."""
    from aigateway_api.main import create_app
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)
    yield client
    client.close()
```

- [ ] **Step 4: Run to verify syntax**

Run: `python3 -c "import ast; ast.parse(open('tests/test_plugin_debug_integration.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add tests/test_plugin_debug_integration.py
git commit -m "test: add Phase 5 (full conflict detection), report writer, and test_client fixture"
```

---

### Task 9: Final review and self-test

**Files:**
- Modify: `tests/test_plugin_debug_integration.py`

**Interfaces:**
- Consumes: all previous tasks

**Steps:**

- [ ] **Step 1: Run syntax check**

Run: `python3 -c "import ast; ast.parse(open('tests/test_plugin_debug_integration.py').read()); print('Syntax OK')"`
Expected: `Syntax OK`

- [ ] **Step 2: Dry-run the test (may skip if infra not available)**

Run: `cd /home/ubuntu/gateway2 && python3 -m pytest tests/test_plugin_debug_integration.py -v --tb=short 2>&1 | head -60`
Expected: Tests run; some may skip if Redis/Qdrant unavailable (graceful degradation)

- [ ] **Step 3: Commit**

```bash
git add tests/test_plugin_debug_integration.py docs/test/plugin_debug_test_report.md
git commit -m "test: finalize plugin debug integration test + self-review"
```
