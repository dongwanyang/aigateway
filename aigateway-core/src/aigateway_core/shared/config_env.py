"""Pure helpers for building the effective gateway configuration."""
from __future__ import annotations

import copy
import json
import os
import re
from collections.abc import Mapping
from typing import Any

_ENV_PREFIX = "AI_GATEWAY_"
_ENV_REFERENCE = re.compile(r"\$\{([^}]+)\}")
_NON_CONFIG_ENV_KEYS = {
    "AI_GATEWAY_BASE_PATH",
    "AI_GATEWAY_CONFIG_PATH",
    "AI_GATEWAY_CONFIG_TEMPLATE_PATH",
    "AI_GATEWAY_CUDA_MEMORY_FRACTION",
    "AI_GATEWAY_ENV",
    "AI_GATEWAY_INITIAL_ADMIN_PASSWORD",
    "AI_GATEWAY_ADMIN_PASSWORD",
    "AI_GATEWAY_PREFILL_INITIAL_CREDENTIALS",
    "AI_GATEWAY_CONSOLE_CHAT_API_KEY",
    "AI_GATEWAY_TRUSTED_PROXY_IPS",
    "AI_GATEWAY_TRUST_PROXY_HEADERS",
}
_ENV_PATH_ALIASES = {
    "AI_GATEWAY_REDIS_URL": "infrastructure.redis.url",
    "AI_GATEWAY_QDRANT_URL": "infrastructure.qdrant.url",
    "AI_GATEWAY_LOG_LEVEL": "observability.log_level",
    "AI_GATEWAY_AUTH_DB_PATH": "auth.database_path",
    "AI_GATEWAY_CORS_ORIGINS": "server.cors_origins",
    "AI_GATEWAY_GENERATION_OPTIMIZATION_DRAFT_WORKFLOW_STORE_DIR": (
        "generation_optimization.draft_workflow.store_dir"
    ),
}


def _environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return environ if environ is not None else os.environ


def parse_env_value(value: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        pass
    lowered = value.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _child_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _candidate_keys(
    current: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> list[str]:
    keys = {str(key) for key in current} | {str(key) for key in schema}
    return sorted(
        keys,
        key=lambda key: len(key.split("_")),
        reverse=True,
    )


def _infer_path(
    tokens: list[str],
    current: Mapping[str, Any],
    schema: Mapping[str, Any],
    index: int = 0,
) -> list[str] | None:
    if index >= len(tokens):
        return []
    for key in _candidate_keys(current, schema):
        key_tokens = key.lower().split("_")
        end = index + len(key_tokens)
        if tokens[index:end] != key_tokens:
            continue
        if end == len(tokens):
            return [key]
        child_path = _infer_path(
            tokens,
            _child_mapping(current.get(key)),
            _child_mapping(schema.get(key)),
            end,
        )
        if child_path is not None:
            return [key, *child_path]
    return None


def env_key_to_config_path(
    env_key: str,
    config: Mapping[str, Any],
    *,
    schema: Mapping[str, Any] | None = None,
) -> str | None:
    if not env_key.startswith(_ENV_PREFIX) or env_key in _NON_CONFIG_ENV_KEYS:
        return None
    alias = _ENV_PATH_ALIASES.get(env_key)
    if alias:
        return alias
    suffix = env_key[len(_ENV_PREFIX) :]
    if "__" in suffix:
        parts = [part.lower() for part in suffix.split("__") if part]
        return ".".join(parts) if parts else None
    tokens = [part for part in suffix.lower().split("_") if part]
    inferred = _infer_path(tokens, config, schema or {}) if tokens else None
    return ".".join(inferred) if inferred else None


def set_nested(config: dict[str, Any], dotted_path: str, value: Any) -> None:
    keys = dotted_path.split(".")
    current = config
    for key in keys[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[keys[-1]] = value


def apply_env_overrides(
    config: dict[str, Any],
    *,
    schema: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    source = _environment(environ)
    applied: list[tuple[str, str]] = []
    for env_key in sorted(
        key for key in source if key.startswith(_ENV_PREFIX)
    ):
        path = env_key_to_config_path(env_key, config, schema=schema)
        if not path:
            continue
        parsed = parse_env_value(source[env_key])
        if path == "server.cors_origins" and isinstance(parsed, str):
            parsed = [
                item.strip() for item in parsed.split(",") if item.strip()
            ]
        set_nested(config, path, parsed)
        applied.append((env_key, path))
    return config, applied


def resolve_env_references(
    data: Any,
    *,
    environ: Mapping[str, str] | None = None,
) -> Any:
    source = _environment(environ)
    if isinstance(data, str):

        def replace(match: re.Match[str]) -> str:
            expression = match.group(1)
            if ":-" in expression:
                name, default = expression.split(":-", 1)
                selected = source.get(name.strip())
                return selected if selected else default
            name = expression.strip()
            return source.get(name, match.group(0))

        return _ENV_REFERENCE.sub(replace, data)
    if isinstance(data, dict):
        return {
            key: resolve_env_references(value, environ=source)
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [
            resolve_env_references(value, environ=source)
            for value in data
        ]
    return data


def apply_environment_mode(
    config: dict[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    source = _environment(environ)
    mode = source.get("AI_GATEWAY_ENV", "development")
    if mode == "production":
        config["debug_mode"] = False
        observability = config.setdefault("observability", {})
        if (
            isinstance(observability, dict)
            and str(observability.get("log_level", "info")).lower() == "debug"
        ):
            observability["log_level"] = "info"
    elif mode == "development":
        config.setdefault("hot_reload", True)
        config.setdefault("debug_mode", True)
    return config


def build_effective_config(
    raw: Mapping[str, Any],
    *,
    schema: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Return one consistently resolved config for all runtime consumers."""
    config = copy.deepcopy(dict(raw))
    config, applied = apply_env_overrides(
        config,
        schema=schema,
        environ=environ,
    )
    resolved = resolve_env_references(config, environ=environ)
    if not isinstance(resolved, dict):
        resolved = config
    return apply_environment_mode(resolved, environ=environ), applied


__all__ = [
    "apply_environment_mode",
    "apply_env_overrides",
    "build_effective_config",
    "env_key_to_config_path",
    "parse_env_value",
    "resolve_env_references",
    "set_nested",
]
