"""Backward-compat shim.

The real ``PluginRegistry`` implementation now lives in the shared runtime
layer at ``aigateway_core.shared.plugin_registry``. This root module
re-exports the full public surface for legacy import paths
(``from aigateway_core.plugin_registry import PluginRegistry``).
"""
from aigateway_core.shared.plugin_registry import (  # noqa: F401
    PluginRegistration,
    PluginRegistry,
)

__all__ = ["PluginRegistration", "PluginRegistry"]
