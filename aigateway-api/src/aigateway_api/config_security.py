"""Security and transaction helpers for control-panel configuration APIs."""
from __future__ import annotations

import copy
import errno
import fcntl
import hashlib
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
    "secret_access_key",
    "secret_key",
    "signing_key",
    "encryption_key",
    "credential",
    "credentials",
}
_SENSITIVE_SUFFIXES = (
    "_api_key",
    "_token",
    "_password",
    "_secret",
    "_secret_key",
    "_private_key",
    "_access_key",
    "_credential",
    "_credentials",
)
_SENSITIVE_LIST_NAMES = {"api_keys", "tokens", "passwords", "secrets"}
_MISSING = object()


class ConfigUpdateBusyError(RuntimeError):
    pass


class ConfigValidationError(ValueError):
    def __init__(self, issues: list[dict[str, Any]]):
        super().__init__("configuration validation failed")
        self.issues = issues


class ConfigVersionConflictError(RuntimeError):
    def __init__(self, expected: str, current: str):
        super().__init__("configuration changed since it was loaded")
        self.expected = expected
        self.current = current


class ConfigPreconditionRequiredError(RuntimeError):
    pass


class ConfigCommit(dict[str, Any]):
    """Committed persisted config with the exact revision created under lock."""

    def __init__(self, config: dict[str, Any], revision: str):
        super().__init__(config)
        self.revision = revision


def config_revision_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_bytes_locked(path: str) -> bytes:
    with open(path, "rb") as file:
        fcntl.flock(file.fileno(), fcntl.LOCK_SH)
        try:
            return file.read()
        finally:
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def config_revision(path: str) -> str:
    return config_revision_bytes(_read_bytes_locked(path))


def _parse_yaml_payload(payload: bytes) -> dict[str, Any]:
    try:
        data = yaml.safe_load(payload.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigValidationError(
            [{"level": "ERROR", "message": "config file is not valid YAML"}]
        ) from exc
    if not isinstance(data, dict):
        raise ConfigValidationError(
            [{"level": "ERROR", "message": "config root must be an object"}]
        )
    return data


def read_versioned_yaml_config(path: str) -> tuple[dict[str, Any], str]:
    payload = _read_bytes_locked(path)
    return _parse_yaml_payload(payload), config_revision_bytes(payload)


def read_yaml_config(path: str) -> dict[str, Any]:
    return read_versioned_yaml_config(path)[0]


def _normalized_name(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def _is_sensitive_path(path: tuple[str, ...]) -> bool:
    if not path:
        return False
    name = _normalized_name(path[-1])
    ancestors = {_normalized_name(part) for part in path[:-1]}
    if name.isdigit() and path[:-1]:
        parent = _normalized_name(path[-2])
        if parent in _SENSITIVE_LIST_NAMES:
            return True
    if name == "key":
        return "api_keys" in ancestors
    return name in _SENSITIVE_NAMES or name.endswith(_SENSITIVE_SUFFIXES)


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
    """Recursively mask persisted secrets, including env-expression defaults."""
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
        if (_is_sensitive_path(path) and value) or _uri_contains_credentials(value):
            return MASKED_SECRET
    return value


def _legacy_masked(value: Any, path: tuple[str, ...]) -> bool:
    return (
        isinstance(value, str)
        and value.endswith("***")
        and _is_sensitive_path(path)
    )


def _contains_masked(value: Any, path: tuple[str, ...]) -> bool:
    if isinstance(value, dict):
        return any(
            _contains_masked(child, (*path, str(key)))
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(
            _contains_masked(child, (*path, str(index)))
            for index, child in enumerate(value)
        )
    return value == MASKED_SECRET or _legacy_masked(value, path)


def _matching_list_item(
    candidate: Any,
    current: list[Any],
    index: int,
    path: tuple[str, ...],
) -> Any:
    fallback = current[index] if index < len(current) else None
    if not isinstance(candidate, dict):
        return _MISSING if _contains_masked(candidate, path) else fallback

    for key in ("id", "key_id", "name", "user_id"):
        identity = candidate.get(key)
        if not isinstance(identity, str) or not identity:
            continue
        matches = [
            item
            for item in current
            if isinstance(item, dict) and item.get(key) == identity
        ]
        return matches[0] if len(matches) == 1 else _MISSING
    if _contains_masked(candidate, path):
        return _MISSING
    return fallback


def _path_text(path: tuple[str, ...]) -> str:
    return ".".join(path) if path else "config"


def restore_masked_values(
    candidate: Any,
    current: Any,
    path: tuple[str, ...] = (),
) -> Any:
    """Replace masks only when they map to one unambiguous persisted secret."""
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
                _matching_list_item(
                    child,
                    existing,
                    index,
                    (*path, str(index)),
                ),
                (*path, str(index)),
            )
            for index, child in enumerate(candidate)
        ]
    if candidate == MASKED_SECRET or _legacy_masked(candidate, path):
        if current is _MISSING or current is None:
            raise ConfigValidationError(
                [
                    {
                        "level": "ERROR",
                        "message": (
                            f"{_path_text(path)}: masked secret has no "
                            "unambiguous persisted value"
                        ),
                    }
                ]
            )
        return copy.deepcopy(current)
    return candidate


def _resolved_config(
    config_manager: Any,
    candidate: dict[str, Any],
    *,
    apply_overrides: bool,
) -> dict[str, Any]:
    resolved = copy.deepcopy(candidate)
    if apply_overrides:
        resolved = config_manager._apply_env_overrides(resolved)
    resolved = config_manager._resolve_env_vars_in_values(resolved)
    return config_manager._apply_environment_mode(resolved)


def validate_candidate(
    config_manager: Any,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Strictly validate persisted and effective component configuration."""
    from aigateway_core.shared.strict_config_validation import (
        validate_component_config_strict,
    )

    persisted = _resolved_config(
        config_manager,
        candidate,
        apply_overrides=False,
    )
    effective = _resolved_config(
        config_manager,
        candidate,
        apply_overrides=True,
    )
    collected = [
        *config_manager._validate_config_strict(persisted),
        *validate_component_config_strict(
            persisted,
            apply_specific_env=False,
        ),
        *config_manager._validate_config_strict(effective),
        *validate_component_config_strict(
            effective,
            apply_specific_env=True,
        ),
    ]

    issues: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for issue in collected:
        marker = (
            str(issue.get("level")),
            str(issue.get("message")),
        )
        if marker not in seen:
            issues.append(issue)
            seen.add(marker)
    if any(issue.get("level") == "ERROR" for issue in issues):
        raise ConfigValidationError(issues)
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


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
            existing = os.stat(path)
            os.chmod(temp_path, stat.S_IMODE(existing.st_mode))
            try:
                os.chown(temp_path, existing.st_uid, existing.st_gid)
            except PermissionError:
                pass
        except OSError:
            pass
        try:
            os.replace(temp_path, path)
            _fsync_directory(target.parent)
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


def _assert_revision_unchanged(path: str, expected: str) -> None:
    current = config_revision(path)
    if current != expected:
        raise ConfigVersionConflictError(expected, current)


def transactional_replace_config(
    path: str,
    candidate: dict[str, Any],
    config_manager: Any,
    *,
    expected_revision: str | None = None,
) -> ConfigCommit:
    """Validate, compare-and-swap, persist and reload with rollback."""
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

        old_bytes = _read_bytes_locked(path)
        current_revision = config_revision_bytes(old_bytes)
        if (
            expected_revision is not None
            and expected_revision != current_revision
        ):
            raise ConfigVersionConflictError(
                expected_revision,
                current_revision,
            )
        restored = restore_masked_values(
            copy.deepcopy(candidate),
            _parse_yaml_payload(old_bytes),
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
        committed_revision = config_revision_bytes(payload)
        committed = False
        try:
            # Internal writers honor ``.lock``; this second CAS check also catches
            # editors or deployment agents that do not.
            _assert_revision_unchanged(path, current_revision)
            _write_bytes_atomic(path, payload)
            committed = True
            config_manager.load()
        except Exception as exc:
            if committed:
                after_failure = config_revision(path)
                if after_failure != committed_revision:
                    # An external writer won after our commit. Never overwrite it
                    # with stale rollback bytes; synchronize runtime best-effort.
                    try:
                        config_manager.load()
                    except Exception:
                        pass
                    raise ConfigVersionConflictError(
                        committed_revision,
                        after_failure,
                    ) from exc
                _write_bytes_atomic(path, old_bytes)
                try:
                    config_manager.load()
                except Exception:
                    pass
            raise
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return ConfigCommit(restored, committed_revision)


__all__ = [
    "ConfigCommit",
    "ConfigPreconditionRequiredError",
    "ConfigUpdateBusyError",
    "ConfigValidationError",
    "ConfigVersionConflictError",
    "MASKED_SECRET",
    "config_revision",
    "config_revision_bytes",
    "read_versioned_yaml_config",
    "read_yaml_config",
    "redact_config",
    "restore_masked_values",
    "transactional_replace_config",
    "validate_candidate",
]
