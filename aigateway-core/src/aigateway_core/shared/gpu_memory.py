"""Side-effect-free GPU memory inspection helpers.

The helpers in this module never initialize a PyTorch CUDA context merely to
answer a status request. Device-wide memory comes from ``nvidia-smi`` when it is
available; process allocator counters are read only after Torch has already
initialized CUDA for real work.
"""
from __future__ import annotations

import csv
import os
import subprocess
from typing import Any

_MIB = 1024 * 1024


def _cuda_intentionally_disabled() -> bool:
    """Return whether this process was explicitly denied CUDA devices."""
    visible = os.getenv("CUDA_VISIBLE_DEVICES", "").strip().lower()
    return visible in {"-1", "none", "void"}


def nvidia_smi_status() -> dict[str, Any] | None:
    """Read device-wide memory without creating a CUDA context."""
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.used,memory.free,memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None

    rows = [row for row in csv.reader(completed.stdout.splitlines()) if len(row) >= 5]
    if not rows:
        return None
    visible = os.getenv("CUDA_VISIBLE_DEVICES", "").split(",", 1)[0].strip()
    selected = next(
        (row for row in rows if visible and row[0].strip() == visible),
        rows[0],
    )
    try:
        used_mib = int(selected[2].strip())
        free_mib = int(selected[3].strip())
        total_mib = int(selected[4].strip())
    except ValueError:
        return None
    return {
        "available": True,
        "device": selected[0].strip(),
        "name": selected[1].strip(),
        "device_used_bytes": used_mib * _MIB,
        "device_free_bytes": free_mib * _MIB,
        "device_total_bytes": total_mib * _MIB,
        "device_memory_source": "nvidia-smi",
    }


def gateway_cuda_status() -> dict[str, Any]:
    """Report Gateway allocator memory without initializing CUDA."""
    device_status = nvidia_smi_status() or {}
    result: dict[str, Any] = {
        "available": bool(device_status),
        "device": None,
        "name": None,
        "allocated_bytes": 0,
        "reserved_bytes": 0,
        "device_used_bytes": 0,
        "device_free_bytes": 0,
        "device_total_bytes": 0,
        "device_memory_source": None,
        "torch_initialized": False,
        "cuda_disabled": _cuda_intentionally_disabled(),
        **device_status,
    }
    try:
        import torch
    except ImportError:
        if not result["available"]:
            result["error"] = "torch_unavailable"
        return result

    # current_device(), mem_get_info() and get_device_name() can create a CUDA
    # context. Polling status must never be the reason an idle process uses VRAM.
    if not torch.cuda.is_initialized():
        if not result["available"]:
            result["error"] = "gpu_status_unavailable"
        return result

    device = torch.cuda.current_device()
    result.update(
        {
            "available": True,
            "torch_initialized": True,
            "device": result.get("device") if device_status else device,
            "name": result.get("name") or torch.cuda.get_device_name(device),
            "allocated_bytes": int(torch.cuda.memory_allocated(device)),
            "reserved_bytes": int(torch.cuda.memory_reserved(device)),
        }
    )
    if not device_status:
        free_bytes, total_bytes = torch.cuda.mem_get_info(device)
        result.update(
            {
                "device_used_bytes": int(total_bytes - free_bytes),
                "device_free_bytes": int(free_bytes),
                "device_total_bytes": int(total_bytes),
                "device_memory_source": "torch",
            }
        )
    return result


def integer_value(mapping: Any, *keys: str) -> int | None:
    """Return the first non-negative numeric field from a mapping."""
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    return None


def comfy_memory(gpu: Any) -> dict[str, Any] | None:
    """Normalize the memory fields returned by ComfyUI's system_stats."""
    if not isinstance(gpu, dict):
        return None
    total = integer_value(gpu, "vram_total", "torch_vram_total")
    free = integer_value(gpu, "vram_free", "torch_vram_free")
    return {
        "raw": gpu,
        "total_bytes": total,
        "free_bytes": free,
        "used_bytes": total - free if total is not None and free is not None else None,
    }


def diagnose_memory(
    gateway: dict[str, Any],
    comfy: dict[str, Any] | None,
    queue_idle: bool | None,
    shared_gpu: bool,
) -> list[str]:
    """Return stable machine-readable explanations for resident memory."""
    findings: list[str] = []
    if shared_gpu:
        findings.append("gateway_and_comfyui_share_one_gpu")
    if gateway.get("torch_initialized"):
        allocated = int(gateway.get("allocated_bytes", 0) or 0)
        reserved = int(gateway.get("reserved_bytes", 0) or 0)
        if reserved > allocated:
            findings.append("gateway_pytorch_cache_reserved")
        if allocated > 0:
            findings.append("gateway_model_memory_resident")
    if queue_idle and comfy and int(comfy.get("used_bytes") or 0) > 0:
        findings.append("comfyui_idle_with_resident_models")
    return findings


__all__ = [
    "comfy_memory",
    "diagnose_memory",
    "gateway_cuda_status",
    "integer_value",
    "nvidia_smi_status",
]
