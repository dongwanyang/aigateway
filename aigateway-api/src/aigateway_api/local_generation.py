"""ComfyUI status and lightweight API-workflow preset storage."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import httpx

_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_CORE_IMAGE_NODES = [
    "CheckpointLoaderSimple",
    "CLIPTextEncode",
    "KSampler",
    "VAEDecode",
    "SaveImage",
]


def builtin_presets(comfy: dict[str, Any]) -> list[dict[str, Any]]:
    """Return immutable built-ins; custom presets are stored separately."""
    checkpoint = str(comfy.get("checkpoint_name", "sd_xl_base_1.0.safetensors"))
    upscale = str(comfy.get("upscale_model", "RealESRGAN_x4plus.pth"))
    return [
        {
            "id": "sdxl-draft",
            "name": "SDXL 图片草稿",
            "kind": "image",
            "builtin": True,
            "enabled": True,
            "languages": ["en"],
            "dependencies": {
                "models": [f"checkpoints/{checkpoint}"],
                "nodes": _CORE_IMAGE_NODES,
            },
        },
        {
            "id": "sdxl-creative-refine",
            "name": "SDXL 创意精修",
            "kind": "image",
            "builtin": True,
            "enabled": True,
            "languages": ["en"],
            "dependencies": {
                "models": [f"checkpoints/{checkpoint}"],
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
            "enabled": bool(comfy.get("qwen_image_enabled", True)),
            "languages": ["zh", "en"],
            "dependencies": {
                "models": [
                    f"diffusion_models/{comfy.get('qwen_image_diffusion_model', 'qwen_image_fp8_e4m3fn.safetensors')}",
                    f"text_encoders/{comfy.get('qwen_image_text_encoder', 'qwen_2.5_vl_7b_fp8_scaled.safetensors')}",
                    f"vae/{comfy.get('qwen_image_vae', 'qwen_image_vae.safetensors')}",
                ],
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
            "enabled": bool(comfy.get("video_enabled", True)),
            "languages": ["en"],
            "dependencies": {
                "models": [
                    f"diffusion_models/{comfy.get('video_diffusion_model', '')}",
                    f"text_encoders/{comfy.get('video_text_encoder', '')}",
                    f"vae/{comfy.get('video_vae', '')}",
                ],
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
            "enabled": bool(comfy.get("upscale_enabled", True)),
            "languages": ["zh", "en"],
            "dependencies": {
                "models": [f"upscale_models/{upscale}"],
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


def preset_store_dir() -> Path:
    return Path(
        os.environ.get(
            "AI_GATEWAY_GENERATION_PRESETS_DIR",
            "/app/data/generation-presets",
        )
    )


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
            presets.append(value)
    return presets


def save_custom_preset(preset: dict[str, Any]) -> None:
    preset_id = str(preset.get("id", ""))
    if not _SAFE_ID.fullmatch(preset_id):
        raise ValueError("preset id must be a safe lowercase identifier")
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
    if not _SAFE_ID.fullmatch(preset_id):
        raise ValueError("invalid preset id")
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
    model_root = Path(models_path)
    return {
        "missing_models": [
            item
            for item in models
            if not isinstance(item, str)
            or item.startswith(("/", "\\"))
            or ".." in Path(item).parts
            or not (model_root / item).is_file()
        ],
        "missing_nodes": [
            item for item in nodes if not isinstance(item, str) or item not in available_nodes
        ],
    }


async def probe_comfyui(comfy: dict[str, Any]) -> dict[str, Any]:
    """Probe only read-only ComfyUI endpoints with bounded timeouts."""
    server_url = str(comfy.get("server_url", "http://localhost:8188")).rstrip("/")
    result: dict[str, Any] = {
        "available": False,
        "manager_enabled": bool(comfy.get("manager_enabled", True)),
        "public_url": str(comfy.get("public_url", "http://localhost:8188")),
        "manager_url": str(comfy.get("public_url", "http://localhost:8188")),
        "gpu": None,
        "queue": None,
        "available_nodes": [],
        "error": None,
    }
    try:
        timeout = httpx.Timeout(
            float(comfy.get("connect_timeout", 10)),
            read=10.0,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            stats_response, object_response, queue_response = await asyncio.gather(
                client.get(f"{server_url}/system_stats"),
                client.get(f"{server_url}/object_info"),
                client.get(f"{server_url}/queue"),
            )
        stats_response.raise_for_status()
        object_response.raise_for_status()
        queue_response.raise_for_status()
        stats = stats_response.json()
        objects = object_response.json()
        queue = queue_response.json()
        devices = stats.get("devices", []) if isinstance(stats, dict) else []
        result.update(
            {
                "available": True,
                "gpu": devices[0] if devices else None,
                "queue": {
                    "running": len(queue.get("queue_running", [])),
                    "pending": len(queue.get("queue_pending", [])),
                },
                "available_nodes": sorted(objects) if isinstance(objects, dict) else [],
            }
        )
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        result["error"] = type(exc).__name__
    models_path = str(comfy.get("models_path", "/comfyui/models"))
    try:
        usage = await asyncio.to_thread(shutil.disk_usage, models_path)
        result["disk"] = {
            "total_bytes": usage.total,
            "free_bytes": usage.free,
        }
    except OSError:
        result["disk"] = None
    return result
