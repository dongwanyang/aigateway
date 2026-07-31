"""Public generation-optimization configuration module.

The compatibility parser lives in ``_config_impl``. This module gives
``DraftWorkflowConfig.store_dir`` a neutral default and keeps parsing/hot reload
behavior explicit without modifying dataclass constructors during package import.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field, fields
from typing import Any

from . import _config_impl as _impl

logger = logging.getLogger(__name__)

for _name in dir(_impl):
    if _name.startswith("__"):
        continue
    globals()[_name] = getattr(_impl, _name)


@dataclass
class DraftWorkflowConfig(_impl.DraftWorkflowConfig):
    """Draft workflow configuration with no inferred deployment directory."""

    store_dir: str = ""


def _draft_from_compat(
    source: _impl.DraftWorkflowConfig,
    store_dir: str,
) -> DraftWorkflowConfig:
    values = {
        item.name: getattr(source, item.name)
        for item in fields(_impl.DraftWorkflowConfig)
    }
    values["store_dir"] = store_dir
    return DraftWorkflowConfig(**values)


@dataclass
class GenerationOptimizationConfig(_impl.GenerationOptimizationConfig):
    """Generation configuration whose Draft storage path is explicitly supplied."""

    draft_workflow: DraftWorkflowConfig = field(
        default_factory=DraftWorkflowConfig
    )

    @classmethod
    def load_from_dict(
        cls,
        data: dict[str, Any],
        previous: GenerationOptimizationConfig | None = None,
    ) -> GenerationOptimizationConfig:
        raw = data if isinstance(data, dict) else {}
        compat = _impl.GenerationOptimizationConfig.load_from_dict(
            raw,
            previous=previous,
        )
        draft_section = raw.get("draft_workflow", {})
        draft_section = (
            draft_section if isinstance(draft_section, dict) else {}
        )
        env_key = (
            "AI_GATEWAY_GENERATION_OPTIMIZATION_"
            "DRAFT_WORKFLOW_STORE_DIR"
        )
        raw_store_dir: Any
        if env_key in os.environ:
            raw_store_dir = os.environ.get(env_key)
        else:
            raw_store_dir = draft_section.get("store_dir")

        if isinstance(raw_store_dir, str) and raw_store_dir.strip():
            selected_store_dir = raw_store_dir.strip()
        elif previous is not None:
            selected_store_dir = str(
                previous.draft_workflow.store_dir or ""
            ).strip()
        else:
            selected_store_dir = ""

        draft_workflow = _draft_from_compat(
            compat.draft_workflow,
            selected_store_dir,
        )
        return cls(
            enabled=compat.enabled,
            ai_director=compat.ai_director,
            model_router=compat.model_router,
            draft_workflow=draft_workflow,
            token_compressor=compat.token_compressor,
            feature_cache=compat.feature_cache,
            cost_tracking=compat.cost_tracking,
            prompt_templates=compat.prompt_templates,
        )

    @classmethod
    def load_from_config_manager(
        cls,
        config_manager: Any,
        previous: GenerationOptimizationConfig | None = None,
    ) -> GenerationOptimizationConfig:
        data = config_manager.get("generation_optimization", {})
        if not isinstance(data, dict):
            logger.error(
                "ConfigManager returned non-object generation_optimization: %s",
                type(data).__name__,
            )
            data = {}
        return cls.load_from_dict(data, previous=previous)


def parse_generation_optimization_config(
    data: dict[str, Any],
    previous: GenerationOptimizationConfig | None = None,
) -> GenerationOptimizationConfig:
    return GenerationOptimizationConfig.load_from_dict(
        data,
        previous=previous,
    )


def validate_generation_optimization_config(
    config: GenerationOptimizationConfig,
) -> list[str]:
    return config.validate()


class GenerationOptimizationConfigWatcher:
    """Thread-safe hot-reload watcher using the public configuration classes."""

    def __init__(
        self,
        config_manager: Any,
        initial_config: GenerationOptimizationConfig | None = None,
    ) -> None:
        self._config_manager = config_manager
        self._lock = threading.RLock()
        self._callbacks: list[Any] = []
        self._current_config = (
            initial_config
            if initial_config is not None
            else GenerationOptimizationConfig.load_from_config_manager(
                config_manager
            )
        )
        if hasattr(config_manager, "on_reload"):
            config_manager.on_reload(self._on_manager_config_reload)

    @property
    def config(self) -> GenerationOptimizationConfig:
        with self._lock:
            return self._current_config

    def reload(self) -> GenerationOptimizationConfig:
        if hasattr(self._config_manager, "snapshot"):
            full_config = self._config_manager.snapshot()
        else:
            full_config = {
                "generation_optimization": self._config_manager.get(
                    "generation_optimization",
                    {},
                )
            }
        self._on_manager_config_reload(full_config)
        return self.config

    def on_change(self, callback: Any) -> None:
        self._callbacks.append(callback)

    @staticmethod
    def _strict_generation_errors(
        new_full_config: dict[str, Any],
    ) -> list[dict[str, Any]]:
        from aigateway_core.shared.strict_config_validation import (
            validate_component_config_strict,
        )

        return [
            issue
            for issue in validate_component_config_strict(
                new_full_config,
                apply_specific_env=True,
            )
            if str(issue.get("message", "")).startswith(
                "generation_optimization"
            )
        ]

    def _on_manager_config_reload(
        self,
        new_full_config: dict[str, Any],
    ) -> None:
        issues = self._strict_generation_errors(new_full_config)
        if issues:
            raise ValueError(
                "; ".join(str(issue.get("message", issue)) for issue in issues)
            )
        raw_section = new_full_config.get("generation_optimization") or {}
        if not isinstance(raw_section, dict):
            raise TypeError("generation_optimization must be an object")
        with self._lock:
            previous = self._current_config
        # ConfigManager callbacks receive a complete file snapshot. Rebuild from
        # defaults so deleting a field has the same effect as restarting.
        new_config = GenerationOptimizationConfig.load_from_dict(
            raw_section,
            previous=None,
        )
        errors = new_config.validate()
        if errors:
            raise ValueError("; ".join(errors))
        self._commit_config(new_config, previous)

    def _on_config_reload(self, new_full_config: dict[str, Any]) -> None:
        """Compatibility seam for callers that submit partial config patches."""
        raw_section = new_full_config.get("generation_optimization")
        if raw_section is None:
            return
        if not isinstance(raw_section, dict):
            logger.error(
                "generation_optimization reload rejected: expected object, got %s",
                type(raw_section).__name__,
            )
            return
        with self._lock:
            previous = self._current_config
        new_config = GenerationOptimizationConfig.load_from_dict(
            raw_section,
            previous=previous,
        )
        errors = new_config.validate()
        if errors:
            for error in errors:
                logger.error(
                    "generation configuration reload rejected: %s",
                    error,
                )
            return
        self._commit_config(new_config, previous)

    def _commit_config(
        self,
        new_config: GenerationOptimizationConfig,
        previous: GenerationOptimizationConfig,
    ) -> None:
        with self._lock:
            self._current_config = new_config
        try:
            self._notify_callbacks(new_config)
        except Exception:
            with self._lock:
                self._current_config = previous
            try:
                self._notify_callbacks(previous)
            except Exception as rollback_exc:
                logger.error(
                    "generation config callback rollback failed: %s",
                    rollback_exc,
                )
            raise

    def _notify_callbacks(
        self,
        new_config: GenerationOptimizationConfig,
    ) -> None:
        errors: list[BaseException] = []
        for callback in tuple(self._callbacks):
            try:
                callback(new_config)
            except Exception as exc:
                logger.exception("generation config callback failed")
                errors.append(exc)
        if errors:
            raise RuntimeError(
                "generation config callback failed: "
                + "; ".join(str(error) for error in errors)
            )


__all__ = [
    name
    for name in globals()
    if not name.startswith("_") and name not in {"Any"}
]
