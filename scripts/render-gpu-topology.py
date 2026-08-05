#!/usr/bin/env python3
"""Generate a logical-index-stable, single-host ComfyUI worker topology."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
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


@contextlib.contextmanager
def _config_write_lock(path: Path):
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as sibling_lock:
        fcntl.flock(sibling_lock.fileno(), fcntl.LOCK_EX)
        try:
            with path.open("r+", encoding="utf-8") as config_handle:
                fcntl.flock(config_handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield config_handle
                finally:
                    fcntl.flock(config_handle.fileno(), fcntl.LOCK_UN)
        finally:
            fcntl.flock(sibling_lock.fileno(), fcntl.LOCK_UN)


def _read_locked_yaml(handle: Any, path: Path) -> dict[str, Any]:
    handle.seek(0)
    loaded = yaml.safe_load(handle.read()) or {}
    if not isinstance(loaded, dict):
        raise RuntimeError(f"expected YAML object: {path}")
    return loaded


def _write_locked_yaml(handle: Any, value: dict[str, Any]) -> None:
    rendered = yaml.safe_dump(value, sort_keys=False, allow_unicode=True)
    handle.seek(0)
    original = handle.read()
    try:
        handle.seek(0)
        handle.write(rendered)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())
    except BaseException:
        try:
            handle.seek(0)
            handle.write(original)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            pass
        raise


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
    *,
    gateway_devices: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    scheduler = scheduler or {}
    dynamic_vram_enabled = scheduler.get(
        "comfyui_dynamic_vram_enabled", False
    )
    if type(dynamic_vram_enabled) is not bool:
        raise ValueError("comfyui_dynamic_vram_enabled must be a boolean")
    disable_dynamic_vram = "false" if dynamic_vram_enabled else "true"
    visible_gateway_devices = devices if gateway_devices is None else gateway_devices
    # Physical UUIDs are stable only for one concrete GPU. Instance replacement
    # changes them, while Docker/NVIDIA logical indices are resolved again on
    # every container start. Persist UUIDs only as runtime telemetry below.
    gateway_visible_devices = ",".join(
        str(int(device["index"])) for device in visible_gateway_devices
    )
    services: dict[str, Any] = {
        "gateway": {
            "environment": {"CUDA_VISIBLE_DEVICES": gateway_visible_devices}
        }
    }
    workers: list[dict[str, Any]] = []
    for position, device in enumerate(devices):
        service = "comfyui" if position == 0 else f"comfyui-gpu-{position}"
        worker_id = f"comfyui-gpu-{position}"
        host_port = 8188 + position
        logical_index = int(device["index"])
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
                "CUDA_VISIBLE_DEVICES": str(logical_index),
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
                "logical_index": logical_index,
                "device_uuid": device["uuid"],
                "server_url": f"http://{service}:8188",
                "public_url": f"http://localhost:{host_port}",
                "capabilities": ["image", "video", "upscale"],
                "memory_total_gb": round(device.get("memory_total_mb", 0) / 1024, 3),
            }
        )
    return {"services": services}, workers


def _runtime_inventory(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Serialize host-discovered devices for Gateway-side topology fallback."""
    result: list[dict[str, Any]] = []
    for device in devices:
        uuid = str(device.get("uuid") or "").strip()
        if not uuid:
            continue
        try:
            index = int(device.get("index", 0))
            total_memory_gb = round(
                max(0, int(device.get("memory_total_mb", 0))) / 1024,
                3,
            )
        except (TypeError, ValueError):
            continue
        result.append(
            {
                "index": index,
                "uuid": uuid,
                "name": str(device.get("name") or "NVIDIA GPU"),
                "total_memory_gb": total_memory_gb,
                # Runtime worker probes replace this optimistic bootstrap value
                # before the coordinator starts accepting generation work.
                "free_memory_gb": total_memory_gb,
            }
        )
    return result


def _previous_index_by_uuid(scheduler: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    devices = scheduler.get("devices", [])
    if not isinstance(devices, list):
        return result
    for item in devices:
        if not isinstance(item, dict) or not item.get("uuid"):
            continue
        raw_index = item.get("index", item.get("logical_index"))
        if raw_index is None:
            continue
        try:
            result[str(item["uuid"])] = int(raw_index)
        except (TypeError, ValueError):
            continue
    return result


def _remap_uuid_selector(
    value: Any,
    *,
    current_by_uuid: dict[str, dict[str, Any]],
    current_by_index: dict[int, dict[str, Any]],
    previous_index_by_uuid: dict[str, int],
    field: str,
) -> Any:
    if value in (None, "auto"):
        return "auto"
    if not isinstance(value, list):
        raise RuntimeError(f"{field} must be 'auto' or a UUID list")

    remapped: list[str] = []
    unresolved: list[str] = []
    for raw in value:
        uuid = str(raw)
        if uuid in current_by_uuid:
            remapped.append(uuid)
            continue
        old_index = previous_index_by_uuid.get(uuid)
        replacement = current_by_index.get(old_index) if old_index is not None else None
        if replacement is None:
            unresolved.append(uuid)
        else:
            remapped.append(str(replacement["uuid"]))
    if unresolved:
        raise RuntimeError(
            f"{field} references devices that cannot be remapped: "
            + ", ".join(sorted(unresolved))
        )
    return list(dict.fromkeys(remapped))


def reconcile_scheduler_device_references(
    inventory: list[dict[str, Any]], scheduler: dict[str, Any]
) -> dict[str, Any]:
    """Refresh persisted UUID policy using its previous logical GPU slots."""
    current_by_uuid = {
        str(item.get("uuid")): item for item in inventory if item.get("uuid")
    }
    current_by_index = {
        int(item["index"]): item for item in inventory if item.get("uuid")
    }
    previous_index_by_uuid = _previous_index_by_uuid(scheduler)
    updated = dict(scheduler)
    for field in ("gateway_devices", "comfyui_devices"):
        updated[field] = _remap_uuid_selector(
            scheduler.get(field, "auto"),
            current_by_uuid=current_by_uuid,
            current_by_index=current_by_index,
            previous_index_by_uuid=previous_index_by_uuid,
            field=field,
        )

    overrides = scheduler.get("device_overrides", [])
    if not isinstance(overrides, list):
        raise RuntimeError("device_overrides must be a list")
    remapped_overrides: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for position, raw in enumerate(overrides):
        if not isinstance(raw, dict) or not raw.get("uuid"):
            raise RuntimeError(
                "GPU device override is malformed at index " + str(position)
            )
        override = dict(raw)
        uuid = str(override["uuid"])
        if uuid not in current_by_uuid:
            old_index = previous_index_by_uuid.get(uuid)
            replacement = (
                current_by_index.get(old_index) if old_index is not None else None
            )
            if replacement is None:
                unresolved.append(uuid)
                continue
            override["uuid"] = str(replacement["uuid"])
        remapped_overrides.append(override)
    if unresolved:
        raise RuntimeError(
            "device_overrides references devices that cannot be remapped: "
            + ", ".join(sorted(unresolved))
        )
    updated["device_overrides"] = remapped_overrides
    return updated


def _select_devices(
    devices: list[dict[str, Any]],
    scheduler: dict[str, Any],
    selector_name: str,
) -> list[dict[str, Any]]:
    selector = scheduler.get(selector_name, "auto")
    if selector not in (None, "auto") and not isinstance(selector, list):
        raise RuntimeError(f"{selector_name} must be 'auto' or a UUID list")
    selected_uuids = (
        {str(item) for item in selector}
        if isinstance(selector, list)
        else None
    )
    overrides = scheduler.get("device_overrides", [])
    if not isinstance(overrides, list):
        raise RuntimeError("device_overrides must be a list")
    disabled = {
        str(item.get("uuid"))
        for item in overrides
        if isinstance(item, dict) and item.get("enabled") is False
    }
    return [
        item
        for item in devices
        if item.get("uuid") not in disabled
        and (selected_uuids is None or item.get("uuid") in selected_uuids)
    ]


def select_comfyui_devices(
    devices: list[dict[str, Any]], scheduler: dict[str, Any]
) -> list[dict[str, Any]]:
    """Apply the configured ComfyUI UUID pool and enabled overrides."""
    return _select_devices(devices, scheduler, "comfyui_devices")


def select_gateway_devices(
    devices: list[dict[str, Any]], scheduler: dict[str, Any]
) -> list[dict[str, Any]]:
    """Apply the configured Gateway UUID pool and enabled overrides."""
    return _select_devices(devices, scheduler, "gateway_devices")


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

    inventory = devices
    runtime_inventory = _runtime_inventory(inventory)
    if not runtime_inventory:
        raise SystemExit("no valid NVIDIA GPU UUIDs discovered")

    with _config_write_lock(args.runtime_config) as config_handle:
        runtime = _read_locked_yaml(config_handle, args.runtime_config)
        raw_scheduler = runtime.setdefault("gpu_scheduler", {})
        if not isinstance(raw_scheduler, dict):
            raise RuntimeError("gpu_scheduler must be an object")
        scheduler = reconcile_scheduler_device_references(
            inventory, raw_scheduler
        )
        devices = select_comfyui_devices(inventory, scheduler)
        if not devices:
            raise SystemExit(
                "configured ComfyUI GPU UUID pool has no available devices"
            )
        compose, workers = render_topology(
            devices,
            scheduler,
            gateway_devices=inventory,
        )
        scheduler.update(
            {
                "enabled": scheduler.get("enabled", True),
                "policy": scheduler.get("policy", "auto"),
                "gateway_devices": scheduler.get("gateway_devices", "auto"),
                "comfyui_devices": scheduler.get("comfyui_devices", "auto"),
                "inventory_source": "host_generated",
                "devices": runtime_inventory,
                "workers": workers,
            }
        )
        runtime["gpu_scheduler"] = scheduler
        _atomic_yaml(args.output_compose, compose)
        _write_locked_yaml(config_handle, runtime)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
