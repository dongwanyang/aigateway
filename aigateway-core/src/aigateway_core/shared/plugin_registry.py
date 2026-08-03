"""Public plugin-registry surface with race-safe runtime instances.

The registry implementation remains in ``_plugin_registry_impl``. This facade
serializes first construction with register/unregister so heavyweight plugins are
constructed exactly once and an instance from an obsolete registration can never
be published under a newly registered plugin with the same name.
"""
from __future__ import annotations

import logging
from typing import Any

from . import _plugin_registry_impl as _impl

logger = logging.getLogger(__name__)

PluginRegistration = _impl.PluginRegistration


class PluginRegistry(_impl.PluginRegistry):
    """Plugin registry with one runtime instance per live registration."""

    def _get_or_create_instance(
        self,
        reg: PluginRegistration,
    ) -> Any | None:
        # Plugin constructors may allocate model memory, background threads,
        # sockets or file handles. Holding the registry lock during the one-time
        # construction is preferable to constructing duplicate disposable
        # instances under concurrent health/engine queries.
        with self._lock:
            current = self._registrations.get(reg.name)
            if current is not reg:
                return None
            cached = self._instances.get(reg.name)
            if cached is not None:
                return cached
            try:
                instance = reg.plugin_class(**reg.config)
            except TypeError as exc:
                logger.warning(
                    "插件 '%s' 实例化失败（配置参数不匹配）: %s",
                    reg.name,
                    exc,
                )
                return None
            self._instances[reg.name] = instance
            return instance

    def get(self, name: str) -> PluginRegistration | None:
        """Return a registration snapshot under the registry lock."""
        with self._lock:
            return self._registrations.get(name)


for _name in dir(_impl):
    if _name.startswith("__") or _name in {"PluginRegistration", "PluginRegistry"}:
        continue
    globals()[_name] = getattr(_impl, _name)


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


__all__ = ("PluginRegistration", "PluginRegistry")
