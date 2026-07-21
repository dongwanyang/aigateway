"""Tests for ModelSelector — picks a cheap text-capable model for internal calls.

These tests mock the REAL ProviderCooldownTracker API (``get_all_status``)
which returns per-model ``{state, state_value, failure_count, last_failure_time,
last_success_time, cooldown_until}``. The brief's fictional ``is_healthy`` /
``get_stats`` returning ``success_rate``/``avg_latency_ms`` do NOT exist on the
real class and are intentionally NOT used here.
"""
import asyncio
import os
import sys
from unittest.mock import MagicMock, AsyncMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.route.model_resolution.model_selector import ModelSelector


def _make_bridge():
    """Bridge mock with the real cooldown API surface."""
    bridge = MagicMock()
    bridge._model_capabilities = {
        "agnes-2.0-flash": ["text", "image", "video"],
        "agnes-image-2.1-flash": ["image"],
        "deepseek-v4-flash": ["text"],
    }
    bridge._model_pricing = {
        "agnes-2.0-flash": {"prompt": 0.02, "completion": 1.0},
        "deepseek-v4-flash": {"prompt": 0.01, "completion": 0.5},
    }
    bridge.get_registered_models = MagicMock(
        return_value=["agnes-2.0-flash", "agnes-image-2.1-flash", "deepseek-v4-flash"]
    )
    cooldown = MagicMock()
    # Real API: one call returns all models; each entry has the real shape.
    # All CLOSED with failure_count=0 → fully healthy.
    cooldown.get_all_status = MagicMock(
        return_value={
            "agnes-2.0-flash": {
                "state": "CLOSED",
                "state_value": 0,
                "failure_count": 0,
                "last_failure_time": 0.0,
                "last_success_time": 0.0,
                "cooldown_until": None,
            },
            "deepseek-v4-flash": {
                "state": "CLOSED",
                "state_value": 0,
                "failure_count": 0,
                "last_failure_time": 0.0,
                "last_success_time": 0.0,
                "cooldown_until": None,
            },
        }
    )
    bridge._cooldown_tracker = cooldown
    return bridge


@pytest.mark.asyncio
async def test_selects_text_capable_model():
    """All CLOSED/healthy → returns a text-capable model (never the image-only one)."""
    bridge = _make_bridge()
    sel = ModelSelector(
        bridge=bridge,
        config={"latency_weight": 0.4, "cost_weight": 0.2, "success_rate_weight": 0.4},
    )
    model = await sel.select_text_model()
    assert model in ("agnes-2.0-flash", "deepseek-v4-flash")


@pytest.mark.asyncio
async def test_excludes_non_text_models():
    """An image-only model must never be selected."""
    bridge = _make_bridge()
    sel = ModelSelector(bridge=bridge, config={})
    for _ in range(5):
        assert await sel.select_text_model() != "agnes-image-2.1-flash"


@pytest.mark.asyncio
async def test_fallback_to_default_when_pool_empty():
    """No text models in capabilities → returns the configured default_model."""
    bridge = _make_bridge()
    bridge._model_capabilities = {"agnes-image-2.1-flash": ["image"]}
    sel = ModelSelector(bridge=bridge, config={}, default_model="agnes-2.0-flash")
    assert await sel.select_text_model() == "agnes-2.0-flash"


@pytest.mark.asyncio
async def test_prefers_healthy_cheap_model():
    """agnes-2.0-flash OPEN (unhealthy) → selector picks deepseek-v4-flash.

    Also verifies cost tie-break: when both healthy, deepseek (cheaper) wins.
    """
    bridge = _make_bridge()

    # --- Case A: agnes-2.0-flash OPEN/unhealthy, deepseek CLOSED/healthy ---
    cooldown_a = MagicMock()
    cooldown_a.get_all_status = MagicMock(
        return_value={
            "agnes-2.0-flash": {
                "state": "OPEN",
                "state_value": 1,
                "failure_count": 5,
                "last_failure_time": 0.0,
                "last_success_time": 0.0,
                "cooldown_until": 9999.0,
            },
            "deepseek-v4-flash": {
                "state": "CLOSED",
                "state_value": 0,
                "failure_count": 0,
                "last_failure_time": 0.0,
                "last_success_time": 0.0,
                "cooldown_until": None,
            },
        }
    )
    bridge._cooldown_tracker = cooldown_a
    sel = ModelSelector(
        bridge=bridge,
        config={"latency_weight": 0.4, "cost_weight": 0.2, "success_rate_weight": 0.4},
    )
    assert await sel.select_text_model() == "deepseek-v4-flash"

    # --- Case B: both healthy, deepseek cheaper → cost tie-break picks deepseek ---
    cooldown_b = MagicMock()
    cooldown_b.get_all_status = MagicMock(
        return_value={
            "agnes-2.0-flash": {
                "state": "CLOSED",
                "state_value": 0,
                "failure_count": 0,
                "last_failure_time": 0.0,
                "last_success_time": 0.0,
                "cooldown_until": None,
            },
            "deepseek-v4-flash": {
                "state": "CLOSED",
                "state_value": 0,
                "failure_count": 0,
                "last_failure_time": 0.0,
                "last_success_time": 0.0,
                "cooldown_until": None,
            },
        }
    )
    bridge._cooldown_tracker = cooldown_b
    assert await sel.select_text_model() == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_all_unhealthy_falls_back_to_pool_first():
    """All text-pool models OPEN → returns the first text-pool model (NOT default_model).

    Verifies internal calls still proceed even when every text model is unhealthy.
    """
    bridge = _make_bridge()
    cooldown = MagicMock()
    cooldown.get_all_status = MagicMock(
        return_value={
            "agnes-2.0-flash": {
                "state": "OPEN",
                "state_value": 1,
                "failure_count": 5,
                "last_failure_time": 0.0,
                "last_success_time": 0.0,
                "cooldown_until": 9999.0,
            },
            "deepseek-v4-flash": {
                "state": "OPEN",
                "state_value": 1,
                "failure_count": 5,
                "last_failure_time": 0.0,
                "last_success_time": 0.0,
                "cooldown_until": 9999.0,
            },
        }
    )
    bridge._cooldown_tracker = cooldown
    # Use a default_model distinct from every pool model so that "fell back to
    # pool first" is unambiguous — returning the default would be a bug here.
    sel = ModelSelector(bridge=bridge, config={}, default_model="gpt-4o-fallback")
    result = await sel.select_text_model()
    # Must be the first text-pool model (agnes-2.0-flash), NOT the default.
    assert result == "agnes-2.0-flash"
    assert result != sel._default_model


@pytest.mark.asyncio
async def test_select_timeout_returns_default_model():
    """Timeout in health check should return default_model."""
    bridge = _make_bridge()
    cooldown = MagicMock()
    # Override _select to hang past the timeout, triggering asyncio.wait_for timeout
    async def slow_select(*a, **k):
        import asyncio
        await asyncio.sleep(10)
        return "never"
    sel = ModelSelector(bridge=bridge, config={}, default_model="fallback-model", timeout_seconds=0.05)
    sel._select = slow_select
    result = await sel.select_text_model()
    assert result == "fallback-model"
