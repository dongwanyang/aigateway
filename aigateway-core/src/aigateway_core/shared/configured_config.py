"""ConfigManager variant using the shared environment-path resolver."""
from __future__ import annotations

import logging
from typing import Any

from .config import ConfigManager as _BaseConfigManager
from .config import _DEFAULT_CONFIG
from .config_env import (
    apply_env_overrides,
    parse_env_value,
    resolve_env_references,
)

logger = logging.getLogger(__name__)


class ConfigManager(_BaseConfigManager):
    """Apply only recognized nested overrides instead of flat pseudo-keys."""

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

    @staticmethod
    def _parse_env_value(value: str) -> Any:
        return parse_env_value(value)


__all__ = ["ConfigManager"]
