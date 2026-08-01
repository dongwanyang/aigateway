"""Safe discovery and selection identifiers for local ComfyUI checkpoints."""

from __future__ import annotations

import base64
from pathlib import Path, PurePosixPath

CHECKPOINT_PRESET_PREFIX = "checkpoint."
_CHECKPOINT_EXTENSIONS = frozenset({".ckpt", ".safetensors"})
_MAX_PRESET_ID_LENGTH = 512
_MAX_DISCOVERED_CHECKPOINTS = 1000


def _normalize_checkpoint_name(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("invalid checkpoint name")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("invalid checkpoint name")
    if path.suffix.lower() not in _CHECKPOINT_EXTENSIONS:
        raise ValueError("unsupported checkpoint extension")
    return path.as_posix()


def checkpoint_preset_id(checkpoint_name: str) -> str:
    """Encode a relative checkpoint path into an opaque, request-safe preset ID."""
    normalized = _normalize_checkpoint_name(checkpoint_name)
    encoded = base64.urlsafe_b64encode(normalized.encode("utf-8")).decode("ascii")
    preset_id = f"{CHECKPOINT_PRESET_PREFIX}{encoded.rstrip('=')}"
    if len(preset_id) > _MAX_PRESET_ID_LENGTH:
        raise ValueError("checkpoint path is too long")
    return preset_id


def checkpoint_name_from_preset_id(preset_id: str | None) -> str | None:
    """Decode a discovered-checkpoint preset ID, returning ``None`` for other IDs."""
    if not isinstance(preset_id, str) or not preset_id.startswith(
        CHECKPOINT_PRESET_PREFIX
    ):
        return None
    if len(preset_id) > _MAX_PRESET_ID_LENGTH:
        raise ValueError("checkpoint preset id is too long")
    payload = preset_id[len(CHECKPOINT_PRESET_PREFIX) :]
    if not payload:
        raise ValueError("invalid checkpoint preset id")
    try:
        padded = payload + "=" * (-len(payload) % 4)
        decoded = base64.b64decode(
            padded.encode("ascii"), altchars=b"-_", validate=True
        ).decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("invalid checkpoint preset id") from exc
    return _normalize_checkpoint_name(decoded)


def validate_checkpoint_file(models_path: str, checkpoint_name: str) -> str:
    """Validate that a checkpoint resolves to a regular file below ``checkpoints``."""
    normalized = _normalize_checkpoint_name(checkpoint_name)
    root = (Path(models_path) / "checkpoints").resolve()
    try:
        target = (root / PurePosixPath(normalized)).resolve(strict=True)
    except OSError as exc:
        raise ValueError("checkpoint file does not exist") from exc
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("checkpoint escapes the configured models directory") from exc
    if not target.is_file():
        raise ValueError("checkpoint is not a regular file")
    return normalized


def discover_checkpoint_models(models_path: str) -> list[str]:
    """Return installed checkpoint paths that are safe for the standard workflow."""
    root = Path(models_path) / "checkpoints"
    if not root.is_dir():
        return []

    discovered: list[str] = []
    for candidate in root.rglob("*"):
        if len(discovered) >= _MAX_DISCOVERED_CHECKPOINTS:
            break
        if candidate.suffix.lower() not in _CHECKPOINT_EXTENSIONS:
            continue
        try:
            relative_name = candidate.relative_to(root).as_posix()
            discovered.append(validate_checkpoint_file(models_path, relative_name))
        except (OSError, ValueError):
            continue
    return sorted(set(discovered), key=str.casefold)
