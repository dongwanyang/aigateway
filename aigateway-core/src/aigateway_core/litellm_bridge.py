"""LiteLLM bridge — compatibility shim.

The authoritative implementation now lives in the unified route layer:
``aigateway_core.route.bridge.litellm_bridge`` (LiteLLMBridge) and
``aigateway_core.route.bridge.cooldown`` (ProviderCooldownTracker).

This module re-exports them so legacy imports
``from aigateway_core.litellm_bridge import LiteLLMBridge`` keep working.
"""
from aigateway_core.route.bridge.cooldown import ProviderCooldownTracker
from aigateway_core.route.bridge.litellm_bridge import (
    LiteLLMBridge,
    _emit_bridge_debug,
)

__all__ = ["LiteLLMBridge", "ProviderCooldownTracker"]
