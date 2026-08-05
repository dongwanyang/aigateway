"""Reconcile persisted GPU topology before CUDA or the scheduler initializes.

Physical NVIDIA UUIDs change when an installation moves to a different GPU or
cloud instance.  The persisted scheduler topology therefore treats logical GPU
indices as the stable intent and refreshes UUID telemetry from ``nvidia-smi`` at
process start.  This module runs before ``aigateway_api.main`` imports PyTorch.
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

    devices: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",", 4)]
        if len(parts) != 5:
            continue
        try:
            devices.append(
                {
                    "index": int(parts[0]),
                    "uuid": parts[1],
                    "name": parts[2],
                    "total_memory_gb": round(float(parts[3]) / 1024, 3),
                    "free_memory_gb": round(float(parts[4]) / 1024, 3),
                }
            )
        except ValueError:
            continue
    return sorted(devices, key=lambda item: int(item["index"]))


def _inventory_fingerprint(devices: list[dict[str, Any]]) -> str:
    value = [
        {
            "index": item["index"],
            "uuid": item["uuid"],
            "name": item["name"],
            "total_memory_gb": item["total_memory_gb"],
        }
        for item in devices
    ]
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
        try:
            result[str(item["uuid"])] = int(
                item.get("index", item.get("logical_index", 0))
            )
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
        return value

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
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict) or not raw.get("uuid"):
            continue
        override = dict(raw)
        uuid = str(override["uuid"])
        if uuid not in current_by_uuid:
            old_index = previous_index_by_uuid.get(uuid)
            replacement = (
                current_by_index.get(old_index) if old_index is not None else None
            )
            if replacement is None:
                logger.warning(
                    "Dropping stale GPU device override during topology bootstrap",
                    extra={"device_uuid": uuid},
                )
                continue
            override["uuid"] = str(replacement["uuid"])
        result.append(override)
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
    raw_workers = [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
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


def _validate_topology(
    scheduler: dict[str, Any], devices: list[dict[str, Any]]
) -> None:
    device_uuids = {str(item["uuid"]) for item in devices}
    workers = scheduler.get("workers", [])
    worker_uuids = {
        str(item.get("device_uuid"))
        for item in workers
        if isinstance(item, dict) and item.get("device_uuid")
    } if isinstance(workers, list) else set()
    missing = worker_uuids - device_uuids
    if missing:
        raise RuntimeError(
            "GPU scheduler topology incomplete; worker UUIDs are absent from "
            "the current local inventory: " + ", ".join(sorted(missing))
        )


def _normalize_cuda_visible_devices(devices: list[dict[str, Any]]) -> None:
    current = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip()
    if not current:
        return
    tokens = [token.strip() for token in current.split(",") if token.strip()]
    current_indices = {str(int(item["index"])) for item in devices}
    uses_uuid = any(token.startswith("GPU-") for token in tokens)
    invalid_index = any(
        not token.isdigit() or token not in current_indices for token in tokens
    )
    if uses_uuid or invalid_index:
        normalized = ",".join(
            str(int(item["index"])) for item in devices
        )
        os.environ["CUDA_VISIBLE_DEVICES"] = normalized
        logger.warning(
            "Replaced stale CUDA_VISIBLE_DEVICES with current logical indices",
            extra={"previous": current, "current": normalized},
        )


def bootstrap_gpu_topology() -> bool:
    """Refresh UUID telemetry and worker mappings before scheduler startup."""
    devices = _discover_devices()
    if not devices:
        return False
    _normalize_cuda_visible_devices(devices)

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
            runtime = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            if not isinstance(runtime, dict):
                raise RuntimeError("GPU runtime configuration must be a YAML object")
            scheduler = runtime.get("gpu_scheduler")
            if not isinstance(scheduler, dict) or scheduler.get("enabled", True) is False:
                return False

            current_by_uuid = {str(item["uuid"]): item for item in devices}
            current_by_index = {int(item["index"]): item for item in devices}
            previous_index_by_uuid = _previous_index_by_uuid(scheduler)
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
            updated["devices"] = devices
            updated["inventory_source"] = "gateway_startup_discovery"
            updated["inventory_fingerprint"] = _inventory_fingerprint(devices)
            _validate_topology(updated, devices)

            if updated == scheduler:
                return False
            runtime["gpu_scheduler"] = updated
            rendered = yaml.safe_dump(
                runtime, sort_keys=False, allow_unicode=True
            )
            with config_path.open("w", encoding="utf-8") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            logger.warning(
                "GPU topology reconciled before scheduler startup",
                extra={
                    "device_count": len(devices),
                    "worker_count": len(updated["workers"]),
                },
            )
            return True
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


__all__ = ["bootstrap_gpu_topology"]
