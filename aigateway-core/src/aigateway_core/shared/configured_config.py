"""Canonical ConfigManager behavior for strict, deterministic reloads."""
from __future__ import annotations

import copy
import logging
from typing import Any

from .config import ConfigManager as _BaseConfigManager
from .config import _DEFAULT_CONFIG, parse_integration_configs
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


class ConfigStrictValidationError(ValueError):
    """Raised when a complete runtime configuration is invalid."""

    def __init__(self, issues: list[dict[str, Any]]):
        super().__init__(
            "configuration validation failed: "
            + "; ".join(str(issue.get("message", issue)) for issue in issues)
        )
        self.issues = issues


class ConfigManager(_BaseConfigManager):
    """Config manager with one resolver and restart-equivalent full reloads."""

    def load(self) -> dict[str, Any]:
        old_config = copy.deepcopy(getattr(self, "_config", {}))
        old_integrations = getattr(self, "_integration_configs", None)

        config = self._load_yaml(self.config_path)
        config = self._apply_env_overrides(config)
        config = self._resolve_env_vars_in_values(config)
        config = self._apply_environment_mode(config)

        from .strict_config_validation import validate_component_config_strict

        issues = [
            *self._validate_config_strict(config),
            *validate_component_config_strict(
                config,
                apply_specific_env=True,
            ),
        ]
        errors = [issue for issue in issues if issue.get("level") == "ERROR"]
        if errors:
            raise ConfigStrictValidationError(errors)

        self._validate_config(config)
        with self._lock:
            self._config = copy.deepcopy(config)
        try:
            # A file load is a full snapshot, not a partial patch. Rebuilding from
            # defaults makes deletion behave identically before and after restart.
            self._integration_configs = parse_integration_configs(config, None)
            if old_config:
                self._notify_reload(old_config, config)
        except Exception:
            with self._lock:
                self._config = old_config
            self._integration_configs = old_integrations
            if old_config:
                try:
                    self._notify_reload(config, old_config)
                except Exception as rollback_exc:
                    logger.error(
                        "configuration callback rollback failed: %s",
                        rollback_exc,
                    )
            raise

        logger.info(
            "配置已加载: path=%s, keys=%s",
            self.config_path,
            list(config.keys()),
        )
        return self._config

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


__all__ = [
    "ConfigManager",
    "ConfigReloadCallbackError",
    "ConfigStrictValidationError",
]
