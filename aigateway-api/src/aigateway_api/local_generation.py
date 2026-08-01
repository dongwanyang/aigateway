"""ComfyUI status and lightweight API-workflow preset storage."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import httpx
from aigateway_core.shared.comfyui_model_discovery import (
    CHECKPOINT_PRESET_PREFIX,
    checkpoint_preset_id,
    discover_checkpoint_models,
)
from aigateway_core.shared.runtime_values import configured_path

_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_CORE_IMAGE_NODES = [
    "CheckpointLoaderSimple",
    "CLIPTextEncode",
    "KSampler",
    "VAEDecode",
    "SaveImage",
]


def _config_text(config: dict[str, Any], key: str) -> str:
    """Return a trimmed configured string without deployment-specific fallback."""
    value = config.get(key)
    return value.strip() if isinstance(value, str) else ""


def _config_number(
    config: dict[str, Any],
    key: str,
    *,
    fallback_key: str | None = None,
) -> float:
    """Read a required numeric setting, optionally reusing another configured key."""
    if key in config:
        raw_value = config[key]
    elif fallback_key and fallback_key in config:
        raw_value = config[fallback_key]
    else:
        raise ValueError(f"config_missing:{key}")
    value = float(raw_value)
    if value <= 0:
        raise ValueError(f"config_invalid:{key}")
    return value


def _preset_model_requirements(
    requirements: list[tuple[str, str, str]],
    *,
    feature_enabled: bool = True,
) -> tuple[list[str], list[str]]:
    """Return configured model paths and explicit missing-config errors."""
    models: list[str] = []
    errors: list[str] = []
    for folder, config_key, value in requirements:
        if value:
            models.append(f"{folder}/{value}")
        elif feature_enabled:
            errors.append(f"config_missing:{config_key}")
    return models, errors


def _preset_status(feature_enabled: bool, errors: list[str]) -> str:
    if not feature_enabled:
        return "disabled"
    if errors:
        return "configuration_error"
    return "ready"


def _checkpoint_vram_requirement(
    comfy: dict[str, Any], checkpoint_name: str
) -> float | None:
    """Return a trusted server-side VRAM minimum for one checkpoint."""
    raw_mapping = comfy.get("checkpoint_vram_gb", {})
    if not isinstance(raw_mapping, dict) or checkpoint_name not in raw_mapping:
        return None
    try:
        value = float(raw_mapping[checkpoint_name])
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def validate_custom_preset_id(preset_id: str) -> str:
    """Validate custom IDs and keep runtime-generated namespaces reserved."""
    if isinstance(preset_id, str) and preset_id.startswith(CHECKPOINT_PRESET_PREFIX):
        raise ValueError("preset id uses reserved checkpoint namespace")
    if not isinstance(preset_id, str) or not _SAFE_ID.fullmatch(preset_id):
        raise ValueError("preset id must be a safe lowercase identifier")
    return preset_id


def builtin_presets(comfy: dict[str, Any]) -> list[dict[str, Any]]:
    """Return immutable built-ins with explicit configuration state.

    Model filenames are deployment configuration. Empty values never become
    synthetic paths such as ``checkpoints/``; the corresponding preset is marked
    ``configuration_error`` and dependency validation remains fail-closed.
    """
    checkpoint = _config_text(comfy, "checkpoint_name")
    upscale = _config_text(comfy, "upscale_model")
    qwen_diffusion = _config_text(comfy, "qwen_image_diffusion_model")
    qwen_encoder = _config_text(comfy, "qwen_image_text_encoder")
    qwen_vae = _config_text(comfy, "qwen_image_vae")
    video_diffusion = _config_text(comfy, "video_diffusion_model")
    video_encoder = _config_text(comfy, "video_text_encoder")
    video_vae = _config_text(comfy, "video_vae")

    sdxl_models, sdxl_errors = _preset_model_requirements(
        [("checkpoints", "checkpoint_name", checkpoint)]
    )
    allowed_checkpoints = {
        item
        for item in comfy.get("allowed_checkpoints", [])
        if isinstance(item, str) and item
    }
    if checkpoint and checkpoint not in allowed_checkpoints:
        sdxl_errors.append(f"checkpoint_not_allowlisted:{checkpoint}")
    qwen_enabled = bool(comfy.get("qwen_image_enabled", False))
    qwen_models, qwen_errors = _preset_model_requirements(
        [
            ("diffusion_models", "qwen_image_diffusion_model", qwen_diffusion),
            ("text_encoders", "qwen_image_text_encoder", qwen_encoder),
            ("vae", "qwen_image_vae", qwen_vae),
        ],
        feature_enabled=qwen_enabled,
    )
    video_enabled = bool(comfy.get("video_enabled", False))
    video_models, video_errors = _preset_model_requirements(
        [
            ("diffusion_models", "video_diffusion_model", video_diffusion),
            ("text_encoders", "video_text_encoder", video_encoder),
            ("vae", "video_vae", video_vae),
        ],
        feature_enabled=video_enabled,
    )
    upscale_enabled = bool(comfy.get("upscale_enabled", False))
    upscale_models, upscale_errors = _preset_model_requirements(
        [("upscale_models", "upscale_model", upscale)],
        feature_enabled=upscale_enabled,
    )

    presets = [
        {
            "id": "sdxl-draft",
            "name": "SDXL 图片草稿",
            "kind": "image",
            "builtin": True,
            "enabled": not sdxl_errors,
            "configuration_status": _preset_status(True, sdxl_errors),
            "configuration_errors": sdxl_errors,
            "languages": ["en"],
            "required_vram_gb": float(comfy.get("sdxl_required_vram_gb", 8.0)),
            "dependencies": {
                "models": sdxl_models,
                "nodes": _CORE_IMAGE_NODES,
            },
        },
        {
            "id": "sdxl-creative-refine",
            "name": "SDXL 创意精修",
            "kind": "image",
            "builtin": True,
            "enabled": not sdxl_errors,
            "configuration_status": _preset_status(True, sdxl_errors),
            "configuration_errors": list(sdxl_errors),
            "languages": ["en"],
            "required_vram_gb": float(comfy.get("sdxl_required_vram_gb", 8.0)),
            "dependencies": {
                "models": list(sdxl_models),
                "nodes": [
                    "LoadImage",
                    "ImageScale",
                    *_CORE_IMAGE_NODES,
                    "VAEEncode",
                ],
            },
        },
        {
            "id": "qwen-image",
            "name": "Qwen-Image 中文/英文图片",
            "kind": "image",
            "builtin": True,
            "enabled": qwen_enabled and not qwen_errors,
            "configuration_status": _preset_status(qwen_enabled, qwen_errors),
            "configuration_errors": qwen_errors,
            "languages": ["zh", "en"],
            "required_vram_gb": float(
                comfy.get("qwen_image_required_vram_gb", 12.0)
            ),
            "dependencies": {
                "models": qwen_models,
                "nodes": [
                    "UNETLoader",
                    "CLIPLoader",
                    "VAELoader",
                    "CLIPTextEncode",
                    "ModelSamplingAuraFlow",
                    "EmptySD3LatentImage",
                    "KSampler",
                    "VAEDecode",
                    "SaveImage",
                ],
            },
        },
        {
            "id": "wan2.2-ti2v-5b",
            "name": "Wan2.2 图片关键帧到视频",
            "kind": "video",
            "builtin": True,
            "enabled": video_enabled and not video_errors,
            "configuration_status": _preset_status(video_enabled, video_errors),
            "configuration_errors": video_errors,
            "languages": ["en"],
            "required_vram_gb": float(comfy.get("video_required_vram_gb", 12.0)),
            "dependencies": {
                "models": video_models,
                "nodes": [
                    "UNETLoader",
                    "CLIPLoader",
                    "VAELoader",
                    "WanImageToVideo",
                    "SaveAnimatedWEBP",
                ],
            },
        },
        {
            "id": "realesrgan-faithful-4k",
            "name": "RealESRGAN 4K 保真",
            "kind": "upscale",
            "builtin": True,
            "enabled": upscale_enabled and not upscale_errors,
            "configuration_status": _preset_status(upscale_enabled, upscale_errors),
            "configuration_errors": upscale_errors,
            "languages": ["zh", "en"],
            "required_vram_gb": float(
                comfy.get("upscale_required_vram_gb", 4.0)
            ),
            "dependencies": {
                "models": upscale_models,
                "nodes": [
                    "LoadImage",
                    "UpscaleModelLoader",
                    "ImageUpscaleWithModel",
                    "ImageScale",
                    "SaveImage",
                ],
            },
        },
    ]
    for preset in presets:
        preset["source"] = "builtin"
        preset["selectable"] = True
    return presets


def discovered_checkpoint_presets(comfy: dict[str, Any]) -> list[dict[str, Any]]:
    """Discover files, but expose only explicitly trusted checkpoints as selectable.

    A ``.ckpt``/``.safetensors`` suffix proves only the container format. It does
    not prove compatibility with the standard SDXL workflow or establish a safe
    scheduling budget. Operators must therefore both allowlist a checkpoint and
    configure its minimum VRAM before clients may select it.
    """
    models_path = _config_text(comfy, "models_path")
    if not models_path:
        return []
    configured_checkpoint = _config_text(comfy, "checkpoint_name")
    allowed_checkpoints = {
        item
        for item in comfy.get("allowed_checkpoints", [])
        if isinstance(item, str) and item
    }
    presets: list[dict[str, Any]] = []
    for checkpoint_name in discover_checkpoint_models(models_path):
        if checkpoint_name == configured_checkpoint:
            continue
        try:
            preset_id = checkpoint_preset_id(checkpoint_name)
        except ValueError:
            continue
        errors: list[str] = []
        allowlisted = checkpoint_name in allowed_checkpoints
        if not allowlisted:
            errors.append(f"checkpoint_not_allowlisted:{checkpoint_name}")
        required_vram_gb = _checkpoint_vram_requirement(comfy, checkpoint_name)
        if required_vram_gb is None:
            errors.append(f"checkpoint_vram_unconfigured:{checkpoint_name}")
        selectable = not errors
        presets.append(
            {
                "id": preset_id,
                "name": f"{Path(checkpoint_name).stem}（本地 Checkpoint）",
                "kind": "image",
                "builtin": False,
                "source": "discovered",
                "selectable": selectable,
                "enabled": selectable,
                "configuration_status": (
                    "ready" if selectable else "configuration_error"
                ),
                "configuration_errors": errors,
                "languages": ["zh", "en"],
                "workflow_family": "sdxl" if allowlisted else "unknown",
                "required_vram_gb": required_vram_gb,
                "model_name": checkpoint_name,
                "dependencies": {
                    "models": [f"checkpoints/{checkpoint_name}"],
                    "nodes": list(_CORE_IMAGE_NODES),
                },
            }
        )
    return presets


def preset_store_dir() -> Path:
    explicit = os.environ.get("AI_GATEWAY_GENERATION_PRESETS_DIR", "").strip()
    return Path(explicit or configured_path("generation_optimization.preset_store_dir"))


def load_custom_presets() -> list[dict[str, Any]]:
    root = preset_store_dir()
    if not root.exists():
        return []
    presets: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            preset_id = str(value.get("id", ""))
            try:
                validate_custom_preset_id(preset_id)
            except ValueError:
                continue
            presets.append(value)
    return presets


def merge_generation_presets(
    *groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a deterministic, globally ID-unique preset list.

    Source precedence is the caller's group order (built-in, discovered, custom).
    Invalid or colliding later entries are omitted rather than making selection
    ambiguous.
    """
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for preset in group:
            preset_id = preset.get("id") if isinstance(preset, dict) else None
            if not isinstance(preset_id, str) or not preset_id or preset_id in seen:
                continue
            seen.add(preset_id)
            merged.append(preset)
    return merged


def save_custom_preset(preset: dict[str, Any]) -> None:
    preset_id = validate_custom_preset_id(str(preset.get("id", "")))
    root = preset_store_dir()
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{preset_id}.json"
    fd, temp_name = tempfile.mkstemp(prefix=f".{preset_id}.", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(preset, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def delete_custom_preset(preset_id: str) -> bool:
    preset_id = validate_custom_preset_id(preset_id)
    path = preset_store_dir() / f"{preset_id}.json"
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def dependency_status(
    preset: dict[str, Any],
    models_path: str,
    available_nodes: set[str],
) -> dict[str, list[str]]:
    deps = preset.get("dependencies", {})
    models = deps.get("models", []) if isinstance(deps, dict) else []
    nodes = deps.get("nodes", []) if isinstance(deps, dict) else []
    configuration_errors = preset.get("configuration_errors", [])
    if not isinstance(configuration_errors, list):
        configuration_errors = []
    model_root = Path(models_path)
    missing_models = [
        item
        for item in models
        if not isinstance(item, str)
        or not item
        or item.endswith("/")
        or item.startswith(("/", "\\"))
        or ".." in Path(item).parts
        or not (model_root / item).is_file()
    ]
    missing_models.extend(
        str(error)
        for error in configuration_errors
        if isinstance(error, str) and error
    )
    return {
        "missing_models": missing_models,
        "missing_nodes": [
            item for item in nodes if not isinstance(item, str) or item not in available_nodes
        ],
        "configuration_errors": [
            str(error)
            for error in configuration_errors
            if isinstance(error, str) and error
        ],
    }


async def probe_comfyui(comfy: dict[str, Any]) -> dict[str, Any]:
    """Probe read-only ComfyUI endpoints using only configured deployment values."""
    server_url = _config_text(comfy, "server_url").rstrip("/")
    public_url = _config_text(comfy, "public_url").rstrip("/")
    manager_url = _config_text(comfy, "manager_url").rstrip("/") or public_url
    models_path = _config_text(comfy, "models_path")

    config_errors = [
        f"config_missing:{key}"
        for key, value in (
            ("server_url", server_url),
            ("public_url", public_url),
            ("models_path", models_path),
        )
        if not value
    ]
    preset_errors = {
        error
        for preset in builtin_presets(comfy)
        for error in preset.get("configuration_errors", [])
        if isinstance(error, str) and error
    }
    config_errors.extend(sorted(preset_errors))

    result: dict[str, Any] = {
        "available": False,
        "manager_enabled": bool(comfy.get("manager_enabled", False)),
        "public_url": public_url,
        "manager_url": manager_url,
        "gpu": None,
        "queue": None,
        "available_nodes": [],
        "disk": None,
        "configuration_status": (
            "configuration_error" if config_errors else "configured"
        ),
        "configuration_errors": config_errors,
        "endpoint_errors": {},
        "error": config_errors[0] if config_errors else None,
    }
    if not server_url:
        return result

    try:
        timeout = httpx.Timeout(
            _config_number(comfy, "connect_timeout"),
            read=_config_number(
                comfy,
                "read_timeout",
                fallback_key="execution_timeout",
            ),
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            responses = await asyncio.gather(
                client.get(f"{server_url}/system_stats"),
                client.get(f"{server_url}/object_info"),
                client.get(f"{server_url}/queue"),
                return_exceptions=True,
            )

        payloads: dict[str, Any] = {}
        endpoint_errors: dict[str, str] = {}
        for endpoint, response in zip(
            ("system_stats", "object_info", "queue"),
            responses,
            strict=True,
        ):
            if isinstance(response, BaseException):
                endpoint_errors[endpoint] = type(response).__name__
                continue
            try:
                response.raise_for_status()
                payloads[endpoint] = response.json()
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                endpoint_errors[endpoint] = type(exc).__name__

        stats = payloads.get("system_stats")
        objects = payloads.get("object_info")
        queue = payloads.get("queue")
        devices = stats.get("devices", []) if isinstance(stats, dict) else []
        queue_view = None
        if isinstance(queue, dict):
            queue_view = {
                "running": len(queue.get("queue_running", [])),
                "pending": len(queue.get("queue_pending", [])),
            }
        result.update(
            {
                "available": bool(payloads),
                "gpu": devices[0] if devices else None,
                "queue": queue_view,
                "available_nodes": sorted(objects) if isinstance(objects, dict) else [],
                "endpoint_errors": endpoint_errors,
                "error": (
                    config_errors[0]
                    if config_errors
                    else (next(iter(endpoint_errors.values())) if not payloads and endpoint_errors else None)
                ),
            }
        )
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        result["error"] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__

    if models_path:
        try:
            usage = await asyncio.to_thread(shutil.disk_usage, models_path)
            result["disk"] = {
                "total_bytes": usage.total,
                "free_bytes": usage.free,
            }
        except OSError:
            result["disk"] = None
    return result
