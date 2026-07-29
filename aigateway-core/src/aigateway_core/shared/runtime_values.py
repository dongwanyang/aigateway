"""Small, dependency-light accessors for runtime values stored in config.yaml.

This module is used by low-level components that are created without a
``ConfigManager`` reference. It keeps deployment values in YAML while avoiding
new global clients or import-time service initialization.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from threading import RLock
from typing import Any

import yaml

_LOCK = RLock()
_CACHE_PATH: str | None = None
_CACHE_MTIME_NS: int | None = None
_CACHE_DATA: dict[str, Any] = {}
_SAFE_NAMESPACE = re.compile(r"[^A-Za-z0-9_.-]+")


def _config_path() -> Path:
    return Path(os.environ.get("AI_GATEWAY_CONFIG_PATH", "./config.yaml")).expanduser()


def load_runtime_config() -> dict[str, Any]:
    """Load config.yaml and refresh the cache when the file mtime changes."""
    global _CACHE_PATH, _CACHE_MTIME_NS, _CACHE_DATA

    path = _config_path()
    try:
        stat = path.stat()
    except OSError as exc:
        raise RuntimeError(f"runtime_config_unavailable:{path}") from exc

    resolved = str(path.resolve())
    with _LOCK:
        if _CACHE_PATH == resolved and _CACHE_MTIME_NS == stat.st_mtime_ns:
            return _CACHE_DATA
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise RuntimeError(f"runtime_config_invalid:{path}") from exc
        if not isinstance(raw, dict):
            raise RuntimeError(f"runtime_config_not_object:{path}")
        _CACHE_PATH = resolved
        _CACHE_MTIME_NS = stat.st_mtime_ns
        _CACHE_DATA = raw
        return _CACHE_DATA


def get_runtime_value(path: str, *, required: bool = True) -> Any:
    """Read a dotted configuration path from the current YAML file."""
    value: Any = load_runtime_config()
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            if required:
                raise RuntimeError(f"runtime_config_missing:{path}")
            return None
        value = value[part]
    return value


def configured_path(path: str) -> str:
    """Resolve a configured filesystem path relative to config.yaml.

    Absolute paths are preserved. Relative paths are anchored to the directory
    containing the active configuration file rather than the process working
    directory, making container, systemd and test launches deterministic.
    """
    raw = get_runtime_value(path)
    if not isinstance(raw, str) or not raw.strip():
        raise RuntimeError(f"runtime_config_invalid:{path}")
    value = Path(raw.strip()).expanduser()
    if not value.is_absolute():
        value = _config_path().resolve().parent / value
    return str(value.resolve())


def _namespace() -> str:
    explicit = get_runtime_value("infrastructure.redis.namespace", required=False)
    source = explicit or get_runtime_value("observability.otel_service_name")
    normalized = _SAFE_NAMESPACE.sub("-", str(source).strip()).strip("-:.")
    if not normalized:
        raise RuntimeError("runtime_config_invalid:infrastructure.redis.namespace")
    return normalized


def redis_key_prefix(component: str) -> str:
    """Return an explicit component prefix or derive one from the YAML namespace."""
    overrides = get_runtime_value("infrastructure.redis.key_prefixes", required=False)
    if isinstance(overrides, dict):
        configured = overrides.get(component)
        if isinstance(configured, str) and configured.strip():
            return configured.strip().rstrip(":")

    namespace = _namespace()
    pipeline_version = str(get_runtime_value("cache.pipeline_version")).strip()
    if component == "l2_index":
        return f"{namespace}:l2:idx:v{pipeline_version}"
    if component == "l2_hash":
        return f"{namespace}:cache:v{pipeline_version}search"
    return f"{namespace}:{component}"


def media_cache_ttl_seconds() -> int:
    value = int(get_runtime_value("media_optimization.media_cache_ttl"))
    if value <= 0:
        raise RuntimeError("runtime_config_invalid:media_optimization.media_cache_ttl")
    return value


def configured_model_pricing(model: str) -> dict[str, float] | None:
    """Return model pricing from ``providers.*.model_grouper[].pricing``.

    The function intentionally has no built-in model table. A missing model is
    represented by ``None`` so callers can record an unknown cost explicitly
    instead of silently applying a stale price.
    """
    bare_model = model.split("/")[-1]
    providers = get_runtime_value("providers", required=False)
    if not isinstance(providers, dict):
        return None

    for provider in providers.values():
        if not isinstance(provider, dict):
            continue
        groups = provider.get("model_grouper", [])
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            pricing = group.get("pricing")
            if not isinstance(pricing, dict):
                continue
            entry = pricing.get(model) or pricing.get(bare_model)
            if not isinstance(entry, dict):
                continue
            try:
                prompt = float(entry.get("prompt", 0.0))
                completion = float(entry.get("completion", prompt))
            except (TypeError, ValueError):
                raise RuntimeError(f"runtime_config_invalid:pricing:{bare_model}")
            if prompt < 0 or completion < 0:
                raise RuntimeError(f"runtime_config_invalid:pricing:{bare_model}")
            return {"prompt": prompt, "completion": completion}
    return None
