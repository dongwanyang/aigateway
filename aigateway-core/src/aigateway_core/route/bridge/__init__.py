"""Unified route bridge package.

Re-exports the authoritative implementations from the submodules. The real
classes live in ``.cooldown`` and ``.litellm_bridge``; the root
``aigateway_core.litellm_bridge`` is now a thin compatibility shim that
imports back from here.
"""
from aigateway_core.route.bridge.cooldown import ProviderCooldownTracker
from aigateway_core.route.bridge.litellm_bridge import LiteLLMBridge

__all__ = ["LiteLLMBridge", "ProviderCooldownTracker"]
