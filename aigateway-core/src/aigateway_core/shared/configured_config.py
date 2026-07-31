"""Canonical ConfigManager behavior for environment and reload consistency."""
from __future__ import annotations

import copy
import logging
from typing import Any

from .config import ConfigManager as _BaseConfigManager
from .config import _DEFAULT_CONFIG
from .config_env import (
    apply_environment_mode,
    apply_env_overrides,
    parse_env_value,
    resolve_env_references,
)

logger = logging.getLogger(__name__)


class ConfigReloadCallbackError(RuntimeError):
    """Raised when one or more runtime consumers reject a configuration."""

    def __init__(self, errors: list[BaseException]):
        super().__init__(
            "configuration reload callback failed: "
            + "; ".join(str(error) for error in errors)
        )
        self.errors = errors


class ConfigManager(_BaseConfigManager):
    """Config manager with one environment resolver and observable reload failures."""

    def load(self) -> dict[str, Any]:
        old_config = copy.deepcopy(getattr(self, "_config", {}))
        old_integrations = getattr(self, "_integration_configs", None)
        try:
            return super().load()
        except Exception:
            if hasattr(self, "_lock"):
                with self._lock:
                    self._config = old_config
            self._integration_configs = old_integrations
            raise

    def _apply_env_overrides(self, config: dict[str, Any]) -> dict[str, Any]:
        config, applied = apply_env_overrides(config, schema=_DEFAULT_CONFIG)
        if applied:
            logger.info(
                "环境变量覆盖: %d 个变量应用到配置 (%s)",
                len(applied),
                ", ".join(path for _key, path in applied),
            )
        return config

    def _resolve_env_vars_in_values(
        self,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        resolved = resolve_env_references(config)
        return resolved if isinstance(resolved, dict) else config

    def _apply_environment_mode(
        self,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        return apply_environment_mode(config)

    def _notify_reload(
        self,
        old_config: dict[str, Any],
        new_config: dict[str, Any],
    ) -> None:
        errors: list[BaseException] = []
        for callback in tuple(self._reload_callbacks):
            try:
                callback(new_config)
            except Exception as exc:
                logger.exception("热重载回调执行失败")
                errors.append(exc)
        if errors:
            raise ConfigReloadCallbackError(errors)

    @staticmethod
    def _parse_env_value(value: str) -> Any:
        return parse_env_value(value)


__all__ = ["ConfigManager", "ConfigReloadCallbackError"]
