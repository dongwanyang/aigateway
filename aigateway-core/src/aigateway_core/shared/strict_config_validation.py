"""Strict component validation used before a configuration commit."""
from __future__ import annotations

import dataclasses
from typing import Any

from aigateway_core.pipelines.generation._common import _config_impl as generation_impl

from . import config as config_module
from .integration_configs import (
    CLIPConfig,
    ComfyUIConfig,
    ConvCompressorConfig,
    PaddleOCRConfig,
    PromptCompressConfig,
    RAGRetrieverConfig,
    UnstructuredConfig,
)

_IntegrationSpec = tuple[str, str, type[Any], str]
_INTEGRATION_SPECS: tuple[_IntegrationSpec, ...] = (
    ("plugin", "prompt_compress", PromptCompressConfig, "PROMPT_COMPRESS"),
    (
        "nested",
        "generation_optimization.token_compressor.clip",
        CLIPConfig,
        "CLIP",
    ),
    (
        "nested",
        "generation_optimization.draft_workflow.comfyui",
        ComfyUIConfig,
        "COMFYUI",
    ),
    ("plugin", "rag_retriever", RAGRetrieverConfig, "RAG_RETRIEVER"),
    ("plugin", "conv_compressor", ConvCompressorConfig, "CONV_COMPRESSOR"),
    (
        "nested",
        "media_optimization.image.paddleocr",
        PaddleOCRConfig,
        "PADDLEOCR",
    ),
    (
        "nested",
        "media_optimization.document.unstructured",
        UnstructuredConfig,
        "UNSTRUCTURED",
    ),
)
_OBJECT_SECTIONS = {
    "server",
    "plugin_runtime",
    "retry_budget",
    "intent_classifier",
    "model_selector",
    "task_routing",
    "generation",
    "auth",
    "providers",
    "embedding",
    "observability",
    "infrastructure",
    "cache",
    "circuit_breaker",
    "rate_limiter",
    "streaming",
    "code_rag",
    "media_optimization",
    "generation_optimization",
    "debug",
}
_BOOLEAN_SECTIONS = {"hot_reload", "debug_mode"}
_GENERATION_EXTENSION_FIELDS = {
    "draft_workflow": {"comfyui"},
    "token_compressor": {"clip"},
}


def _error(path: str, message: str) -> dict[str, str]:
    return {"level": "ERROR", "message": f"{path}: {message}"}


def _matches_type(value: Any, type_hint: Any) -> bool:
    """Handle postponed PEP 604 annotations without tolerant coercion."""
    text = str(type_hint).strip().lower().replace("typing.", "")
    optional = "none" in text or "optional" in text
    if value is None:
        return optional
    if "bool" in text:
        return isinstance(value, bool)
    if "int" in text:
        return isinstance(value, int) and not isinstance(value, bool)
    if "float" in text:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if "str" in text:
        return isinstance(value, str)
    if "list" in text:
        return isinstance(value, list)
    if "dict" in text:
        return isinstance(value, dict)
    if "tuple" in text:
        return isinstance(value, (list, tuple))
    return True


def _validate_top_level_structure(
    config: dict[str, Any],
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for section in _OBJECT_SECTIONS:
        if section in config and not isinstance(config[section], dict):
            issues.append(_error(section, "must be an object"))
    for section in _BOOLEAN_SECTIONS:
        if section in config and not isinstance(config[section], bool):
            issues.append(_error(section, "must be a boolean"))
    if "plugins" in config and not isinstance(config["plugins"], list):
        issues.append(_error("plugins", "must be a list"))
    return issues


def _nested_value(
    config: dict[str, Any],
    dotted_path: str,
) -> tuple[bool, Any]:
    current: Any = config
    for part in dotted_path.split("."):
        if not isinstance(current, dict):
            return True, current
        if part not in current:
            return False, None
        current = current[part]
    return True, current


def _plugin_value(
    config: dict[str, Any],
    plugin_name: str,
) -> tuple[bool, Any]:
    plugins = config.get("plugins")
    if not isinstance(plugins, list):
        return False, None
    for item in plugins:
        if isinstance(item, dict) and item.get("name") == plugin_name:
            return True, item.get("config", {})
    return False, None


def _validate_dataclass_values(
    path: str,
    config_class: type[Any],
    values: dict[str, Any],
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    fields = {field.name: field for field in dataclasses.fields(config_class)}
    for name, value in values.items():
        field = fields.get(name)
        if field is None:
            issues.append(_error(f"{path}.{name}", "unknown field"))
            continue
        if not _matches_type(value, field.type):
            issues.append(
                _error(
                    f"{path}.{name}",
                    f"expected {field.type}, received {type(value).__name__}",
                )
            )
            continue
        constraint = config_module._FIELD_VALIDATORS.get((config_class, name))
        if constraint and not config_module._check_constraint(value, constraint):
            issues.append(
                _error(
                    f"{path}.{name}",
                    f"value {value!r} violates {constraint}",
                )
            )
    return issues


def _validate_integrations(
    config: dict[str, Any],
    *,
    apply_specific_env: bool,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    plugins = config.get("plugins")
    if isinstance(plugins, list):
        for index, item in enumerate(plugins):
            if not isinstance(item, dict):
                issues.append(_error(f"plugins.{index}", "must be an object"))
            elif "config" in item and not isinstance(item.get("config"), dict):
                issues.append(
                    _error(f"plugins.{index}.config", "must be an object")
                )

    for source_kind, source, config_class, env_name in _INTEGRATION_SPECS:
        if source_kind == "plugin":
            if plugins is not None and not isinstance(plugins, list):
                continue
            present, raw = _plugin_value(config, source)
            display_path = f'plugins[name="{source}"].config'
        else:
            present, raw = _nested_value(config, source)
            display_path = source
        if not present:
            continue
        if not isinstance(raw, dict):
            issues.append(_error(display_path, "must be an object"))
            continue
        effective = (
            config_module._apply_env_overrides_for_config(env_name, raw)
            if apply_specific_env
            else dict(raw)
        )
        issues.extend(
            _validate_dataclass_values(display_path, config_class, effective)
        )
    return issues


def _validate_generation(
    config: dict[str, Any],
    *,
    apply_specific_env: bool,
) -> list[dict[str, str]]:
    raw = config.get("generation_optimization")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return []

    working = {
        key: dict(value) if isinstance(value, dict) else value
        for key, value in raw.items()
    }
    if apply_specific_env:
        overrides = generation_impl._get_env_overrides()
        top = overrides.get("_top", {})
        if "enabled" in top:
            working["enabled"] = top["enabled"]
        for section_name, values in overrides.items():
            if section_name == "_top" or not values:
                continue
            current = working.get(section_name, {})
            if not isinstance(current, dict):
                current = {}
            working[section_name] = {**current, **values}

    issues: list[dict[str, str]] = []
    if "enabled" in working:
        try:
            generation_impl._coerce_value(
                working["enabled"],
                bool,
                "enabled",
            )
        except (TypeError, ValueError) as exc:
            issues.append(_error("generation_optimization.enabled", str(exc)))

    for section_name, config_class in generation_impl._SUB_CONFIG_CLASSES.items():
        section = working.get(section_name)
        if section is None:
            continue
        path = f"generation_optimization.{section_name}"
        if not isinstance(section, dict):
            issues.append(_error(path, "must be an object"))
            continue
        field_map = {
            field.name: field for field in dataclasses.fields(config_class)
        }
        ignored = _GENERATION_EXTENSION_FIELDS.get(section_name, set())
        for name, value in section.items():
            if name in ignored:
                continue
            field = field_map.get(name)
            if field is None:
                issues.append(_error(f"{path}.{name}", "unknown field"))
                continue
            if (
                section_name == "draft_workflow"
                and name == "store_dir"
                and not isinstance(value, str)
            ):
                issues.append(_error(f"{path}.{name}", "must be a string"))
                continue
            try:
                coerced = generation_impl._coerce_value(
                    value,
                    field.type,
                    name,
                )
            except (TypeError, ValueError) as exc:
                issues.append(_error(f"{path}.{name}", str(exc)))
                continue
            constraint = generation_impl._VALIDATION_RULES.get(
                section_name,
                {},
            ).get(name)
            if constraint is not None:
                minimum, maximum = constraint
                if not isinstance(coerced, (int, float)) or isinstance(
                    coerced,
                    bool,
                ):
                    issues.append(
                        _error(f"{path}.{name}", "must be numeric")
                    )
                elif not minimum <= coerced <= maximum:
                    issues.append(
                        _error(
                            f"{path}.{name}",
                            f"must be between {minimum} and {maximum}",
                        )
                    )
    return issues


def validate_component_config_strict(
    config: dict[str, Any],
    *,
    apply_specific_env: bool,
) -> list[dict[str, str]]:
    """Return component-level errors that tolerant runtime parsers would hide."""
    return [
        *_validate_top_level_structure(config),
        *_validate_integrations(
            config,
            apply_specific_env=apply_specific_env,
        ),
        *_validate_generation(
            config,
            apply_specific_env=apply_specific_env,
        ),
    ]


__all__ = ["validate_component_config_strict"]
