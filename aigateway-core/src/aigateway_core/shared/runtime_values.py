"""Small, dependency-light accessors for effective runtime configuration.

Low-level components do not receive a ``ConfigManager`` reference, but they use
exactly the same environment-path schema and environment-mode transformation.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from threading import RLock
from typing import Any

import yaml

from .config import _DEFAULT_CONFIG
from .config_env import build_effective_config

_LOCK = RLock()
_CACHE_PATH: str | None = None
_CACHE_MTIME_NS: int | None = None
_CACHE_ENV_FINGERPRINT: int | None = None
_CACHE_DATA: dict[str, Any] = {}
_SAFE_NAMESPACE = re.compile(r"[^A-Za-z0-9_.-]+")


def _config_path() -> Path:
    return Path(
        os.environ.get("AI_GATEWAY_CONFIG_PATH", "./config.yaml")
    ).expanduser()


def _environment_fingerprint() -> int:
    return hash(tuple(sorted(os.environ.items())))


def _read_yaml_locked(path: Path) -> tuple[int, dict[str, Any]]:
    """Read one complete inode while honoring the writer's advisory lock."""
    try:
        with path.open("r", encoding="utf-8") as file:
            try:
                import fcntl
            except ImportError:
                mtime_ns = os.fstat(file.fileno()).st_mtime_ns
                raw = yaml.safe_load(file) or {}
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_SH)
                try:
                    mtime_ns = os.fstat(file.fileno()).st_mtime_ns
                    raw = yaml.safe_load(file) or {}
                finally:
                    fcntl.flock(file.fileno(), fcntl.LOCK_UN)
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"runtime_config_invalid:{path}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"runtime_config_not_object:{path}")
    return mtime_ns, raw


def load_runtime_config() -> dict[str, Any]:
    """Load and cache the same effective config observed by ConfigManager."""
    global _CACHE_PATH, _CACHE_MTIME_NS, _CACHE_ENV_FINGERPRINT, _CACHE_DATA

    path = _config_path()
    try:
        observed_stat = path.stat()
    except OSError as exc:
        raise RuntimeError(f"runtime_config_unavailable:{path}") from exc

    resolved = str(path.resolve())
    env_fingerprint = _environment_fingerprint()
    with _LOCK:
        if (
            _CACHE_PATH == resolved
            and _CACHE_MTIME_NS == observed_stat.st_mtime_ns
            and _CACHE_ENV_FINGERPRINT == env_fingerprint
        ):
            return _CACHE_DATA
        locked_mtime_ns, raw = _read_yaml_locked(path)
        effective, _applied = build_effective_config(
            raw,
            schema=_DEFAULT_CONFIG,
        )
        _CACHE_PATH = resolved
        _CACHE_MTIME_NS = locked_mtime_ns
        _CACHE_ENV_FINGERPRINT = env_fingerprint
        _CACHE_DATA = effective
        return _CACHE_DATA


def get_runtime_value(path: str, *, required: bool = True) -> Any:
    value: Any = load_runtime_config()
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            if required:
                raise RuntimeError(f"runtime_config_missing:{path}")
            return None
        value = value[part]
    return value


def configured_text(path: str) -> str:
    raw = get_runtime_value(path)
    if not isinstance(raw, str) or not raw.strip():
        raise RuntimeError(f"runtime_config_missing:{path}")
    return raw.strip()


def configured_number(
    path: str,
    number_type: type[int | float] = float,
):
    raw = get_runtime_value(path)
    try:
        value = number_type(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"runtime_config_invalid:{path}") from exc
    if value <= 0:
        raise RuntimeError(f"runtime_config_invalid:{path}")
    return value


def configured_path(path: str) -> str:
    raw = configured_text(path)
    value = Path(raw).expanduser()
    if not value.is_absolute():
        value = _config_path().resolve().parent / value
    return str(value.resolve())


def _namespace() -> str:
    explicit = get_runtime_value(
        "infrastructure.redis.namespace",
        required=False,
    )
    source = explicit or get_runtime_value("observability.otel_service_name")
    normalized = _SAFE_NAMESPACE.sub("-", str(source).strip()).strip("-:.")
    if not normalized:
        raise RuntimeError(
            "runtime_config_invalid:infrastructure.redis.namespace"
        )
    return normalized


def redis_key_prefix(component: str) -> str:
    overrides = get_runtime_value(
        "infrastructure.redis.key_prefixes",
        required=False,
    )
    if isinstance(overrides, dict):
        configured = overrides.get(component)
        if isinstance(configured, str) and configured.strip():
            return configured.strip().rstrip(":")

    namespace = _namespace()
    pipeline_version = str(
        get_runtime_value("cache.pipeline_version")
    ).strip()
    if component == "l2_index":
        return f"{namespace}:l2:idx:v{pipeline_version}"
    if component == "l2_hash":
        return f"{namespace}:cache:v{pipeline_version}search"
    return f"{namespace}:{component}"


def media_cache_ttl_seconds() -> int:
    return int(
        configured_number("media_optimization.media_cache_ttl", int)
    )


def _group_model_names(group: dict[str, Any]) -> set[str]:
    names: set[str] = set()

    def add_name(value: Any) -> None:
        if isinstance(value, dict):
            value = value.get("name") or value.get("model")
        if not isinstance(value, str) or not value.strip():
            return
        normalized = value.strip()
        names.add(normalized)
        names.add(normalized.split("/")[-1])

    models = group.get("models", [])
    if isinstance(models, list):
        for model in models:
            add_name(model)
    fallbacks = group.get("fallback_models", [])
    if isinstance(fallbacks, list):
        for model in fallbacks:
            add_name(model)
    return names


def configured_model_pricing(model: str) -> dict[str, float] | None:
    bare_model = model.split("/")[-1]
    providers = get_runtime_value("providers", required=False)
    if not isinstance(providers, dict):
        return None

    for provider_name, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        groups = provider.get("model_grouper", [])
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            if bare_model not in _group_model_names(group):
                continue
            pricing = group.get("pricing")
            if not isinstance(pricing, dict):
                continue
            candidates = (
                pricing.get(model),
                pricing.get(bare_model),
                pricing.get(provider_name),
                pricing.get("$default"),
            )
            entry = next(
                (
                    candidate
                    for candidate in candidates
                    if isinstance(candidate, dict)
                    and (
                        "prompt" in candidate
                        or "completion" in candidate
                    )
                ),
                None,
            )
            if entry is None:
                continue
            try:
                prompt = float(entry.get("prompt", 0.0))
                completion = float(entry.get("completion", prompt))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"runtime_config_invalid:pricing:{bare_model}"
                ) from exc
            if prompt < 0 or completion < 0:
                raise RuntimeError(
                    f"runtime_config_invalid:pricing:{bare_model}"
                )
            return {"prompt": prompt, "completion": completion}
    return None
