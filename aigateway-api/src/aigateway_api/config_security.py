"""Security and transaction helpers for control-panel configuration APIs."""
from __future__ import annotations

import copy
import errno
import fcntl
import os
import stat
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

MASKED_SECRET = "***********"
_SENSITIVE_NAMES = {
    "api_key",
    "client_secret",
    "access_token",
    "refresh_token",
    "private_key",
    "password",
    "secret",
    "token",
    "connection_string",
    "dsn",
}


class ConfigUpdateBusyError(RuntimeError):
    pass


class ConfigValidationError(ValueError):
    def __init__(self, issues: list[dict[str, Any]]):
        super().__init__("configuration validation failed")
        self.issues = issues


def read_yaml_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    if not isinstance(data, dict):
        raise ConfigValidationError(
            [{"level": "ERROR", "message": "config root must be an object"}]
        )
    return data


def _normalized_name(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def _is_sensitive_path(path: tuple[str, ...]) -> bool:
    if not path:
        return False
    name = _normalized_name(path[-1])
    if name == "key":
        return "api_keys" in {
            _normalized_name(part) for part in path[:-1]
        }
    return (
        name in _SENSITIVE_NAMES
        or name.endswith(
            ("_api_key", "_token", "_password", "_secret")
        )
    )


def _uri_contains_credentials(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return bool(
        parsed.scheme
        and (parsed.username is not None or parsed.password is not None)
    )


def redact_config(value: Any, path: tuple[str, ...] = ()) -> Any:
    """Recursively mask persisted secrets while preserving env references."""
    if isinstance(value, dict):
        return {
            key: redact_config(child, (*path, str(key)))
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            redact_config(child, (*path, str(index)))
            for index, child in enumerate(value)
        ]
    if isinstance(value, str):
        if value.startswith("${") and value.endswith("}"):
            return value
        if (
            (_is_sensitive_path(path) and value)
            or _uri_contains_credentials(value)
        ):
            return MASKED_SECRET
    return value


def _matching_list_item(
    candidate: Any,
    current: list[Any],
    index: int,
) -> Any:
    fallback = current[index] if index < len(current) else None
    if not isinstance(candidate, dict):
        return fallback
    for key in ("id", "key_id", "name", "user_id"):
        identity = candidate.get(key)
        if not isinstance(identity, str) or not identity:
            continue
        return next(
            (
                item
                for item in current
                if isinstance(item, dict)
                and item.get(key) == identity
            ),
            fallback,
        )
    return fallback


def restore_masked_values(
    candidate: Any,
    current: Any,
    path: tuple[str, ...] = (),
) -> Any:
    """Replace masked placeholders with the corresponding persisted value."""
    if isinstance(candidate, dict):
        existing = current if isinstance(current, dict) else {}
        return {
            key: restore_masked_values(
                child,
                existing.get(key),
                (*path, str(key)),
            )
            for key, child in candidate.items()
        }
    if isinstance(candidate, list):
        existing = current if isinstance(current, list) else []
        return [
            restore_masked_values(
                child,
                _matching_list_item(child, existing, index),
                (*path, str(index)),
            )
            for index, child in enumerate(candidate)
        ]
    legacy_masked = (
        isinstance(candidate, str)
        and candidate.endswith("***")
        and _is_sensitive_path(path)
    )
    if candidate == MASKED_SECRET or legacy_masked:
        return copy.deepcopy(current)
    return candidate


def _strict_issues(
    config_manager: Any,
    candidate: dict[str, Any],
) -> list[dict[str, Any]]:
    resolved = config_manager._resolve_env_vars_in_values(
        copy.deepcopy(candidate)
    )
    resolved = config_manager._apply_environment_mode(resolved)
    return list(config_manager._validate_config_strict(resolved))


def validate_candidate(
    config_manager: Any,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Validate both persisted YAML and the effective env-overridden config."""
    persisted_issues = _strict_issues(config_manager, candidate)
    effective = config_manager._apply_env_overrides(copy.deepcopy(candidate))
    effective = config_manager._resolve_env_vars_in_values(effective)
    effective = config_manager._apply_environment_mode(effective)
    effective_issues = list(
        config_manager._validate_config_strict(effective)
    )

    issues: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for issue in [*persisted_issues, *effective_issues]:
        marker = (
            str(issue.get("level")),
            str(issue.get("message")),
        )
        if marker not in seen:
            issues.append(issue)
            seen.add(marker)
    if any(issue.get("level") == "ERROR" for issue in issues):
        raise ConfigValidationError(issues)

    from aigateway_core.shared.config import parse_integration_configs

    parse_integration_configs(
        effective,
        config_manager.integration_configs,
    )
    return effective


def _write_bytes_inplace(path: str, payload: bytes) -> None:
    with open(path, "r+b") as file:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        try:
            file.seek(0)
            file.truncate()
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        finally:
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _write_bytes_atomic(path: str, payload: bytes) -> None:
    if os.path.ismount(path):
        _write_bytes_inplace(path, payload)
        return
    target = Path(path)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        try:
            os.chmod(temp_path, stat.S_IMODE(os.stat(path).st_mode))
        except OSError:
            pass
        try:
            os.replace(temp_path, path)
        except OSError as exc:
            if exc.errno not in {
                errno.EBUSY,
                errno.EXDEV,
                errno.ENOTSUP,
                errno.EPERM,
            }:
                raise
            _write_bytes_inplace(path, payload)
    finally:
        try:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        except OSError:
            pass


def transactional_replace_config(
    path: str,
    candidate: dict[str, Any],
    config_manager: Any,
) -> dict[str, Any]:
    """Validate, persist and reload a full config with automatic rollback."""
    with open(path + ".lock", "a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(
                lock_file.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            raise ConfigUpdateBusyError(
                "another configuration update is in progress"
            ) from exc

        old_bytes = Path(path).read_bytes()
        restored = restore_masked_values(
            copy.deepcopy(candidate),
            read_yaml_config(path),
        )
        if not isinstance(restored, dict):
            raise ConfigValidationError(
                [
                    {
                        "level": "ERROR",
                        "message": "config root must be an object",
                    }
                ]
            )
        validate_candidate(config_manager, restored)
        payload = yaml.safe_dump(
            restored,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        ).encode("utf-8")
        committed = False
        try:
            _write_bytes_atomic(path, payload)
            committed = True
            config_manager.load()
        except Exception:
            if committed:
                _write_bytes_atomic(path, old_bytes)
                try:
                    config_manager.load()
                except Exception:
                    pass
            raise
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return restored


__all__ = [
    "ConfigUpdateBusyError",
    "ConfigValidationError",
    "MASKED_SECRET",
    "read_yaml_config",
    "redact_config",
    "restore_masked_values",
    "transactional_replace_config",
    "validate_candidate",
]
