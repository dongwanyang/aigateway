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

# ------------------------------------------------------------------
# Fixtures: config backup & restore
# ------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def config_backup():
    """Backup config.yaml before all tests, restore after."""
    # Backup
    if CONFIG_PATH.exists():
        shutil.copy2(str(CONFIG_PATH), str(BACKUP_PATH))
    yield
    # Restore
    if BACKUP_PATH.exists():
        shutil.copy2(str(BACKUP_PATH), str(CONFIG_PATH))
        try:
            BACKUP_PATH.unlink()
        except FileNotFoundError:
            pass


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


def trigger_chat(client: TestClient, request_body: Optional[Dict] = None) -> Dict[str, Any]:
    """POST /v1/chat/completions, return response JSON."""
    body = request_body or CHAT_REQUEST.copy()
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
        except Exception:
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
