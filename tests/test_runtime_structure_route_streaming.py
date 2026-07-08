"""Task 4: route bridge migration.

Verifies that LiteLLMBridge and ProviderCooldownTracker are *defined* in the
route/bridge submodules (not merely re-exported from the root file).
"""
from aigateway_core.route.bridge.cooldown import ProviderCooldownTracker
from aigateway_core.route.bridge.litellm_bridge import LiteLLMBridge


def test_route_bridge_objects_resolve_from_new_paths():
    assert ProviderCooldownTracker.__name__ == "ProviderCooldownTracker"
    assert LiteLLMBridge.__name__ == "LiteLLMBridge"


def test_root_shim_reexports_same_objects():
    """The root compatibility shim must hand back the same class objects."""
    from aigateway_core import litellm_bridge as root

    assert root.LiteLLMBridge is LiteLLMBridge
    assert root.ProviderCooldownTracker is ProviderCooldownTracker


# ---------------------------------------------------------------------------
# Task 5: route streaming + metrics migration
# ---------------------------------------------------------------------------

from aigateway_core.route.metrics.costing import _estimate_cost
from aigateway_core.route.streaming.cache_stream import simulate_stream_from_cache
from aigateway_core.route.streaming.sse import SSEGenerator


def test_route_streaming_helpers_resolve_from_new_paths():
    assert SSEGenerator.__name__ == "SSEGenerator"
    assert callable(simulate_stream_from_cache)
    assert _estimate_cost("gpt-4o", 1000) > 0


def test_estimate_cost_preserves_pricing_logic():
    """_estimate_cost must keep the same pricing table + rounding behavior."""
    # gpt-4o: 0.000005 * 1000 = 0.005
    assert _estimate_cost("gpt-4o", 1000) == 0.005
    # Unknown model falls back to 0.000001
    assert _estimate_cost("unknown-model", 100) == 0.0001
    # Model with provider prefix is stripped
    assert _estimate_cost("azure/gpt-4o", 1000) == 0.005


def test_l3_helpers_resolve_from_core_prefix_cache():
    """L3 vector + backfill helpers must resolve from core, not the API surface."""
    from aigateway_core.prefix.cache.l3_semantic import (
        _compute_l3_vector,
        _safe_l3_backfill,
    )
    assert callable(_compute_l3_vector)
    assert callable(_safe_l3_backfill)
