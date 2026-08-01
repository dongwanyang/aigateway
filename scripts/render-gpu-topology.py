#!/usr/bin/env python3
"""Generate a UUID-stable, single-host ComfyUI worker topology."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml


def discover_devices() -> list[dict[str, Any]]:
    query = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=index,uuid,name,memory.total",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    devices: list[dict[str, Any]] = []
    if query.returncode == 0:
        for line in query.stdout.splitlines():
            parts = [part.strip() for part in line.split(",", 3)]
            if len(parts) != 4:
                continue
            try:
                devices.append(
                    {
                        "index": int(parts[0]),
                        "uuid": parts[1],
                        "name": parts[2],
                        "memory_total_mb": int(parts[3]),
                    }
                )
            except ValueError:
                continue
    if devices:
        return devices

    # Compatibility with older/fake nvidia-smi implementations that only
    # support ``-L`` and the memory.total query independently.
    listed = subprocess.run(
        ["nvidia-smi", "-L"], capture_output=True, text=True, check=False
    )
    memory = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=memory.total",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    memory_values = [
        int(value.strip())
        for value in memory.stdout.splitlines()
        if value.strip().isdigit()
    ]
    pattern = re.compile(r"GPU\s+(\d+):\s*(.*?)\s*\(UUID:\s*([^\)]+)\)")
    for line in listed.stdout.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        index = int(match.group(1))
        devices.append(
            {
                "index": index,
                "uuid": match.group(3).strip(),
                "name": match.group(2).strip(),
                "memory_total_mb": memory_values[index] if index < len(memory_values) else 0,
            }
        )
    return devices


def _atomic_yaml(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            yaml.safe_dump(value, handle, sort_keys=False, allow_unicode=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def render_topology(
    devices: list[dict[str, Any]],
    scheduler: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    scheduler = scheduler or {}
    dynamic_vram_enabled = scheduler.get(
        "comfyui_dynamic_vram_enabled", False
    )
    if type(dynamic_vram_enabled) is not bool:
        raise ValueError("comfyui_dynamic_vram_enabled must be a boolean")
    disable_dynamic_vram = "false" if dynamic_vram_enabled else "true"
    services: dict[str, Any] = {
        "gateway": {"environment": {"CUDA_VISIBLE_DEVICES": "all"}}
    }
    workers: list[dict[str, Any]] = []
    for position, device in enumerate(devices):
        service = "comfyui" if position == 0 else f"comfyui-gpu-{position}"
        worker_id = f"comfyui-gpu-{position}"
        host_port = 8188 + position
        worker_data_prefix = (
            "${AIGATEWAY_COMFY_DATA_DIR:-./comfyui}"
            if position == 0
            else f"${{AIGATEWAY_COMFY_DATA_DIR:-./comfyui}}/workers/{worker_id}"
        )
        volumes = [
            "${AIGATEWAY_COMFY_DATA_DIR:-./comfyui}/models:/opt/ComfyUI/models:ro",
            "${AIGATEWAY_COMFY_DATA_DIR:-./comfyui}/custom_nodes:/opt/ComfyUI/custom_nodes",
            f"{worker_data_prefix}/input:/opt/ComfyUI/input",
            f"{worker_data_prefix}/output:/opt/ComfyUI/output",
            f"{worker_data_prefix}/user:/opt/ComfyUI/user",
            "${AIGATEWAY_COMFY_DATA_DIR:-./comfyui}/workflows:/opt/ComfyUI/user/default/workflows:ro",
        ]
        override = {
            "environment": {
                "CUDA_VISIBLE_DEVICES": device["uuid"],
                "COMFYUI_VRAM_FLAG": "${COMFYUI_VRAM_FLAG:-}",
                "COMFYUI_DISABLE_DYNAMIC_VRAM": (
                    "${COMFYUI_DISABLE_DYNAMIC_VRAM:-"
                    f"{disable_dynamic_vram}}}"
                ),
                "COMFYUI_CORS_ENABLED": "${COMFYUI_CORS_ENABLED:-true}",
                "COMFYUI_CORS_ORIGIN": "${COMFYUI_CORS_ORIGIN:-}",
                "PYTORCH_CUDA_ALLOC_CONF": "${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}",
            },
            "volumes": volumes,
        }
        if position == 0:
            services[service] = override
        else:
            services[service] = {
                "profiles": ["comfy-container"],
                "image": "${COMFYUI_IMAGE:-aigateway-comfyui:local}",
                "pull_policy": "${AIGATEWAY_PULL_POLICY:-build}",
                "build": {
                    # Quickstart loads the repository's docker-compose.yml
                    # first, so relative Compose paths resolve from repo root.
                    "context": ".",
                    "dockerfile": "aigateway-api/Dockerfile",
                    "target": "comfyui",
                    "args": {
                        "COMFYUI_VERSION": "${COMFYUI_VERSION:-v0.28.0}",
                        "COMFYUI_MANAGER_VERSION": "${COMFYUI_MANAGER_VERSION:-4.2.1}",
                    },
                },
                "gpus": "all",
                "ports": [f"${{COMFYUI_HOST_BIND:-127.0.0.1}}:{host_port}:8188"],
                **override,
                "restart": "unless-stopped",
                "networks": ["gateway-net"],
            }
        workers.append(
            {
                "worker_id": worker_id,
                "device_uuid": device["uuid"],
                "server_url": f"http://{service}:8188",
                "public_url": f"http://localhost:{host_port}",
                "capabilities": ["image", "video", "upscale"],
                "memory_total_gb": round(device.get("memory_total_mb", 0) / 1024, 3),
            }
        )
    return {"services": services}, workers


def select_comfyui_devices(
    devices: list[dict[str, Any]], scheduler: dict[str, Any]
) -> list[dict[str, Any]]:
    """Apply the configured UUID pool and enabled overrides to inventory."""
    selector = scheduler.get("comfyui_devices", "auto")
    selected_uuids = (
        {str(item) for item in selector}
        if isinstance(selector, list)
        else None
    )
    disabled = {
        str(item.get("uuid"))
        for item in scheduler.get("device_overrides", [])
        if isinstance(item, dict) and item.get("enabled") is False
    }
    return [
        item
        for item in devices
        if item.get("uuid") not in disabled
        and (selected_uuids is None or item.get("uuid") in selected_uuids)
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-compose", type=Path, required=True)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--inventory", type=Path)
    args = parser.parse_args()
    if args.inventory:
        loaded = yaml.safe_load(args.inventory.read_text(encoding="utf-8")) or []
        devices = loaded if isinstance(loaded, list) else []
    else:
        devices = discover_devices()
    if not devices:
        raise SystemExit("no NVIDIA GPU UUIDs discovered")

    runtime = yaml.safe_load(args.runtime_config.read_text(encoding="utf-8")) or {}
    scheduler = runtime.setdefault("gpu_scheduler", {})
    devices = select_comfyui_devices(devices, scheduler)
    if not devices:
        raise SystemExit("configured ComfyUI GPU UUID pool has no available devices")
    compose, workers = render_topology(devices, scheduler)
    scheduler.update(
        {
            "enabled": scheduler.get("enabled", True),
            "policy": scheduler.get("policy", "auto"),
            "gateway_devices": scheduler.get("gateway_devices", "auto"),
            "comfyui_devices": scheduler.get("comfyui_devices", "auto"),
            "workers": workers,
        }
    )
    _atomic_yaml(args.output_compose, compose)
    _atomic_yaml(args.runtime_config, runtime)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
