"""Reconcile persisted GPU topology before CUDA or the scheduler initializes.

Physical NVIDIA UUIDs change when an installation moves to a different GPU or
cloud instance. The persisted scheduler topology therefore treats logical GPU
indices as the stable intent and refreshes UUID telemetry from ``nvidia-smi`` at
process start. This module runs before ``aigateway_api.main`` imports PyTorch.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)
TOPOLOGY_FIELDS = (
    "gateway_devices",
    "comfyui_devices",
    "device_overrides",
    "comfyui_dynamic_vram_enabled",
)
TOPOLOGY_DEFAULTS: dict[str, Any] = {
    "gateway_devices": "auto",
    "comfyui_devices": "auto",
    "device_overrides": [],
    "comfyui_dynamic_vram_enabled": False,
}


def _discover_devices() -> list[dict[str, Any]]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid,name,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return []
    devices: list[dict[str, Any]] = []
    for line in lines:
        parts = [part.strip() for part in line.split(",", 4)]
        if len(parts) != 5 or not parts[1]:
            return []
        try:
            memory_total_mb = int(float(parts[3]))
            devices.append(
                {
                    "index": int(parts[0]),
                    "uuid": parts[1],
                    "name": parts[2],
                    "memory_total_mb": memory_total_mb,
                    "memory_free_mb": int(float(parts[4])),
                    "total_memory_gb": round(memory_total_mb / 1024, 3),
                }
            )
        except ValueError:
            return []
    indices = [int(item["index"]) for item in devices]
    uuids = [str(item["uuid"]) for item in devices]
    if len(indices) != len(set(indices)) or len(uuids) != len(set(uuids)):
        return []
    return sorted(devices, key=lambda item: int(item["index"]))


def _memory_total_mb(device: dict[str, Any]) -> int:
    raw = device.get("memory_total_mb")
    if raw is not None:
        return max(0, int(raw))
    return max(0, int(round(float(device.get("total_memory_gb", 0)) * 1024)))


def _runtime_inventory(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in devices:
        total_memory_gb = round(_memory_total_mb(item) / 1024, 3)
        result.append(
            {
                "index": int(item["index"]),
                "uuid": str(item["uuid"]),
                "name": str(item.get("name") or "NVIDIA GPU"),
                "total_memory_gb": total_memory_gb,
                # Match render-gpu-topology.py. Worker probes replace this
                # optimistic bootstrap value after the coordinator starts.
                "free_memory_gb": total_memory_gb,
            }
        )
    return result


def _inventory_fingerprint(
    scheduler: dict[str, Any], devices: list[dict[str, Any]]
) -> str:
    # Keep this byte-for-byte compatible with gpu-topology-controller.py so a
    # successful startup repair does not trigger an endless recreate loop.
    value = {
        "config": {
            field: scheduler.get(field, TOPOLOGY_DEFAULTS[field])
            for field in TOPOLOGY_FIELDS
        },
        "inventory": [
            {
                "index": item.get("index"),
                "uuid": item.get("uuid"),
                "name": item.get("name"),
                "memory_total_mb": _memory_total_mb(item),
            }
            for item in devices
        ],
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _previous_index_by_uuid(scheduler: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    raw_devices = scheduler.get("devices", [])
    if not isinstance(raw_devices, list):
        return result
    for item in raw_devices:
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


def _remap_selector(
    value: Any,
    *,
    current_by_uuid: dict[str, dict[str, Any]],
    current_by_index: dict[int, dict[str, Any]],
    previous_index_by_uuid: dict[str, int],
) -> Any:
    if value in (None, "auto"):
        return "auto"
    if not isinstance(value, list):
        raise RuntimeError("GPU topology selector must be 'auto' or a UUID list")

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
            "GPU topology selector references devices that cannot be remapped: "
            + ", ".join(sorted(unresolved))
        )
    return list(dict.fromkeys(remapped))


def _remap_overrides(
    value: Any,
    *,
    current_by_uuid: dict[str, dict[str, Any]],
    current_by_index: dict[int, dict[str, Any]],
    previous_index_by_uuid: dict[str, int],
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise RuntimeError("GPU device_overrides must be a list")

    result: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for position, raw in enumerate(value):
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
        result.append(override)
    if unresolved:
        raise RuntimeError(
            "GPU device overrides reference devices that cannot be remapped: "
            + ", ".join(sorted(unresolved))
        )
    return result


def _worker_logical_index(
    worker: dict[str, Any],
    *,
    previous_index_by_uuid: dict[str, int],
    worker_count: int,
    device_count: int,
) -> int | None:
    raw_index = worker.get("logical_index")
    if raw_index is not None:
        try:
            return int(raw_index)
        except (TypeError, ValueError):
            return None
    old_uuid = str(worker.get("device_uuid") or "")
    if old_uuid in previous_index_by_uuid:
        return previous_index_by_uuid[old_uuid]
    if worker_count == 1 and device_count == 1:
        return 0
    return None


def _remap_workers(
    value: Any,
    *,
    current_by_index: dict[int, dict[str, Any]],
    previous_index_by_uuid: dict[str, int],
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise RuntimeError("GPU scheduler workers must be a list")
    raw_workers: list[dict[str, Any]] = []
    for position, item in enumerate(value):
        if not isinstance(item, dict):
            raise RuntimeError(
                "GPU scheduler worker is malformed at index " + str(position)
            )
        raw_workers.append(item)

    workers: list[dict[str, Any]] = []
    failures: list[str] = []
    for position, raw in enumerate(raw_workers):
        worker = dict(raw)
        logical_index = _worker_logical_index(
            worker,
            previous_index_by_uuid=previous_index_by_uuid,
            worker_count=len(raw_workers),
            device_count=len(current_by_index),
        )
        device = current_by_index.get(logical_index) if logical_index is not None else None
        if device is None:
            failures.append(str(worker.get("worker_id") or f"worker-{position}"))
            continue
        worker["logical_index"] = logical_index
        worker["device_uuid"] = str(device["uuid"])
        workers.append(worker)
    if failures:
        raise RuntimeError(
            "ComfyUI workers cannot be paired with current local GPUs: "
            + ", ".join(failures)
        )
    return workers


def _pool_expected(runtime: dict[str, Any], scheduler: dict[str, Any]) -> bool:
    deployment = runtime.get("deployment", {})
    shared_gpu = (
        bool(deployment.get("shared_gpu", False))
        if isinstance(deployment, dict)
        else False
    )
    generation = runtime.get("generation_optimization", {})
    draft = generation.get("draft_workflow", {}) if isinstance(generation, dict) else {}
    comfy = draft.get("comfyui", {}) if isinstance(draft, dict) else {}
    scheduler_managed = (
        bool(comfy.get("scheduler_managed", False))
        if isinstance(comfy, dict)
        else False
    )
    workers = scheduler.get("workers", [])
    return shared_gpu or scheduler_managed or bool(workers)


def _inventory_required(runtime: dict[str, Any], scheduler: dict[str, Any]) -> bool:
    if _pool_expected(runtime, scheduler):
        return True
    if scheduler.get("devices") or scheduler.get("device_overrides"):
        return True
    for field in ("gateway_devices", "comfyui_devices"):
        selector = scheduler.get(field)
        if isinstance(selector, list) and bool(selector):
            return True
    return False


def _validate_topology(
    scheduler: dict[str, Any],
    devices: list[dict[str, Any]],
    *,
    pool_expected: bool,
) -> None:
    device_uuids = {str(item["uuid"]) for item in devices}
    workers = scheduler.get("workers", [])
    worker_list = (
        [item for item in workers if isinstance(item, dict)]
        if isinstance(workers, list)
        else []
    )
    if pool_expected and not worker_list:
        raise RuntimeError(
            "GPU scheduler topology incomplete; local ComfyUI pool has no workers"
        )
    worker_uuids = {
        str(item.get("device_uuid"))
        for item in worker_list
        if item.get("device_uuid")
    }
    missing = worker_uuids - device_uuids
    if missing:
        raise RuntimeError(
            "GPU scheduler topology incomplete; worker UUIDs are absent from "
            "the current local inventory: " + ", ".join(sorted(missing))
        )
    worker_ids = [str(item.get("worker_id") or "") for item in worker_list]
    if not all(worker_ids) or len(worker_ids) != len(set(worker_ids)):
        raise RuntimeError(
            "GPU scheduler topology incomplete; ComfyUI worker IDs are not unique"
        )
    logical_indices = [item.get("logical_index") for item in worker_list]
    if len(logical_indices) != len(set(logical_indices)):
        raise RuntimeError(
            "GPU scheduler topology incomplete; multiple workers target one GPU"
        )


def _normalize_cuda_visible_devices(
    devices: list[dict[str, Any]],
    *,
    previous_index_by_uuid: dict[str, int],
) -> bool:
    current = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip()
    if not current:
        return False

    tokens = [token.strip() for token in current.split(",") if token.strip()]
    current_uuids = {str(item["uuid"]) for item in devices}
    current_indices = {str(int(item["index"])) for item in devices}
    normalized: list[str] = []
    unresolved: list[str] = []
    for token in tokens:
        if token in current_uuids or token in current_indices:
            normalized.append(token)
            continue

        old_index = previous_index_by_uuid.get(token)
        if old_index is None and len(tokens) == 1 and len(devices) == 1:
            old_index = int(devices[0]["index"])
        replacement = str(old_index) if old_index is not None else None
        if replacement is None or replacement not in current_indices:
            unresolved.append(token)
        else:
            normalized.append(replacement)
    if unresolved:
        raise RuntimeError(
            "CUDA_VISIBLE_DEVICES references devices that cannot be remapped: "
            + ", ".join(sorted(unresolved))
        )

    normalized_value = ",".join(dict.fromkeys(normalized))
    if normalized_value == current:
        return False
    os.environ["CUDA_VISIBLE_DEVICES"] = normalized_value
    logger.warning(
        "Replaced stale CUDA_VISIBLE_DEVICES with current logical indices",
        extra={"previous": current, "current": normalized_value},
    )
    return True


def _write_locked_yaml(
    handle: Any,
    value: dict[str, Any],
    *,
    original_text: str,
) -> None:
    # ``config.yaml`` is a single-file Docker bind mount. Replacing that mount
    # point can fail with EBUSY, so serialize first and update the locked inode.
    rendered = yaml.safe_dump(value, sort_keys=False, allow_unicode=True)
    try:
        handle.seek(0)
        handle.write(rendered)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())
    except BaseException:
        try:
            handle.seek(0)
            handle.write(original_text)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            logger.exception("Failed to restore GPU runtime configuration")
        raise


def bootstrap_gpu_topology() -> bool:
    """Refresh UUID telemetry and worker mappings before scheduler startup."""
    config_path = Path(
        os.environ.get("AI_GATEWAY_CONFIG_PATH", "./config.yaml")
    ).expanduser()
    if not config_path.is_file():
        return False

    lock_path = Path(str(config_path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with config_path.open("r+", encoding="utf-8") as config_handle:
                fcntl.flock(config_handle.fileno(), fcntl.LOCK_EX)
                try:
                    config_handle.seek(0)
                    original_text = config_handle.read()
                    runtime = yaml.safe_load(original_text) or {}
                    if not isinstance(runtime, dict):
                        raise RuntimeError(
                            "GPU runtime configuration must be a YAML object"
                        )
                    scheduler = runtime.get("gpu_scheduler")
                    if (
                        not isinstance(scheduler, dict)
                        or scheduler.get("enabled", True) is False
                    ):
                        return False

                    devices = _discover_devices()
                    if not devices:
                        if _inventory_required(runtime, scheduler):
                            raise RuntimeError(
                                "GPU topology reconciliation requires a current "
                                "NVIDIA inventory"
                            )
                        return False

                    current_by_uuid = {
                        str(item["uuid"]): item for item in devices
                    }
                    current_by_index = {
                        int(item["index"]): item for item in devices
                    }
                    previous_index_by_uuid = _previous_index_by_uuid(scheduler)
                    visibility_changed = _normalize_cuda_visible_devices(
                        devices,
                        previous_index_by_uuid=previous_index_by_uuid,
                    )
                    updated = dict(scheduler)
                    updated["gateway_devices"] = _remap_selector(
                        scheduler.get("gateway_devices", "auto"),
                        current_by_uuid=current_by_uuid,
                        current_by_index=current_by_index,
                        previous_index_by_uuid=previous_index_by_uuid,
                    )
                    updated["comfyui_devices"] = _remap_selector(
                        scheduler.get("comfyui_devices", "auto"),
                        current_by_uuid=current_by_uuid,
                        current_by_index=current_by_index,
                        previous_index_by_uuid=previous_index_by_uuid,
                    )
                    updated["device_overrides"] = _remap_overrides(
                        scheduler.get("device_overrides", []),
                        current_by_uuid=current_by_uuid,
                        current_by_index=current_by_index,
                        previous_index_by_uuid=previous_index_by_uuid,
                    )
                    updated["workers"] = _remap_workers(
                        scheduler.get("workers", []),
                        current_by_index=current_by_index,
                        previous_index_by_uuid=previous_index_by_uuid,
                    )
                    runtime_inventory = _runtime_inventory(devices)
                    updated["devices"] = runtime_inventory
                    updated["inventory_source"] = "host_generated"
                    updated["inventory_fingerprint"] = _inventory_fingerprint(
                        updated, devices
                    )
                    _validate_topology(
                        updated,
                        runtime_inventory,
                        pool_expected=_pool_expected(runtime, scheduler),
                    )

                    if updated == scheduler:
                        return visibility_changed
                    runtime["gpu_scheduler"] = updated
                    _write_locked_yaml(
                        config_handle,
                        runtime,
                        original_text=original_text,
                    )
                    logger.warning(
                        "GPU topology reconciled before scheduler startup",
                        extra={
                            "device_count": len(devices),
                            "worker_count": len(updated["workers"]),
                        },
                    )
                    return True
                finally:
                    fcntl.flock(config_handle.fileno(), fcntl.LOCK_UN)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


__all__ = ["bootstrap_gpu_topology"]
