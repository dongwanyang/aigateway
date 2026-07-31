"""Public LiteLLM bridge module.

The compatibility implementation is isolated in ``_litellm_bridge_impl`` while
the exported class adds config-backed cost accounting. The public ``httpx``
module reference is retained as a stable test and integration seam: patching
``aigateway_core.route.bridge.litellm_bridge.httpx`` affects the shared module
object used by the compatibility implementation.
"""
from . import _litellm_bridge_impl as _impl
from .configured_litellm_bridge import ConfiguredLiteLLMBridge

httpx = _impl.httpx


class LiteLLMBridge(ConfiguredLiteLLMBridge):
    """Stable public bridge class backed by the configured implementation."""


__all__ = ["LiteLLMBridge", "httpx"]
