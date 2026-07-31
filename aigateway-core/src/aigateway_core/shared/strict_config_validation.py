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
_GENERATION_EXTENSION_FIELDS = {
    "draft_workflow": {"comfyui"},
    "token_compressor": {"clip"},
}


def _error(path: str, message: str) -> dict[str, str]:
    return {"level": "ERROR", "message": f"{path}: {message}"}


def _nested_value(
    config: dict[str, Any],
    dotted_path: str,
) -> tuple[bool, Any]:
    current: Any = config
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _plugin_value(
    config: dict[str, Any],
    plugin_name: str,
) -> tuple[bool, Any]:
    plugins = config.get("plugins")
    if plugins is None:
        return False, None
    if not isinstance(plugins, list):
        return True, plugins
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
            continue
        if not config_module._check_type(value, field.type):
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
    for source_kind, source, config_class, env_name in _INTEGRATION_SPECS:
        if source_kind == "plugin":
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
        return [_error("generation_optimization", "must be an object")]

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
