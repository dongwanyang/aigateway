"""Unified route bridge package.

Re-exports the authoritative implementations from the submodules. The real
classes live in ``.cooldown`` and ``.litellm_bridge``; the root
``aigateway_core.litellm_bridge`` is now a thin compatibility shim that
imports back from here.
"""
from aigateway_core.route.bridge.cooldown import ProviderCooldownTracker
from aigateway_core.route.bridge.litellm_bridge import LiteLLMBridge
from aigateway_core.route.metrics.costing import _estimate_cost as _configured_cost


def _estimate_configured_cost(
    self: LiteLLMBridge,
    model: str,
    total_tokens: int,
) -> float:
    """Use the shared config-backed estimator instead of a bridge-local table."""
    return _configured_cost(model, total_tokens)


# Keep the established method API while making config.yaml the single pricing
# source. This adapter can be removed after the large bridge module is split.
LiteLLMBridge._estimate_cost = _estimate_configured_cost  # type: ignore[method-assign]

__all__ = ["LiteLLMBridge", "ProviderCooldownTracker"]
