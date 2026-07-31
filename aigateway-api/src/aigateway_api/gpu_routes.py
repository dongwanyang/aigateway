"""GPU memory diagnostics and explicit idle-memory release controls."""
from __future__ import annotations

import asyncio
import csv
import gc
import os
import subprocess
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from .auth_middleware import authenticate_admin

router = APIRouter()
_MIB = 1024 * 1024


def _comfy_config(request: Request) -> dict[str, Any]:
    manager = getattr(request.app.state, "config_manager", None)
    value = (
        manager.get("generation_optimization.draft_workflow.comfyui", {})
        if manager is not None
        else {}
    )
    return value if isinstance(value, dict) else {}


def _nvidia_smi_status() -> dict[str, Any] | None:
    """Read device-wide memory without creating a CUDA context in this process."""
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


def _gateway_cuda_status() -> dict[str, Any]:
    """Report memory without initializing CUDA merely because the UI polls us."""
    device_status = _nvidia_smi_status() or {}
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
        **device_status,
    }
    try:
        import torch
    except ImportError:
        if not result["available"]:
            result["error"] = "torch_unavailable"
        return result

    # current_device(), mem_get_info() and get_device_name() can create a CUDA
    # context. Polling /admin/gpu/status must never become the reason an idle
    # Gateway starts consuming VRAM.
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


def _integer(mapping: Any, *keys: str) -> int | None:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    return None


def _comfy_memory(gpu: Any) -> dict[str, Any] | None:
    if not isinstance(gpu, dict):
        return None
    total = _integer(gpu, "vram_total", "torch_vram_total")
    free = _integer(gpu, "vram_free", "torch_vram_free")
    return {
        "raw": gpu,
        "total_bytes": total,
        "free_bytes": free,
        "used_bytes": total - free if total is not None and free is not None else None,
    }


async def _probe(request: Request) -> dict[str, Any]:
    from .local_generation import probe_comfyui

    return await probe_comfyui(_comfy_config(request))


def _diagnosis(
    gateway: dict[str, Any],
    comfy_memory: dict[str, Any] | None,
    queue_idle: bool | None,
    shared_gpu: bool,
) -> list[str]:
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
    if queue_idle and comfy_memory and int(comfy_memory.get("used_bytes") or 0) > 0:
        findings.append("comfyui_idle_with_resident_models")
    return findings


@router.get("/gpu/status")
async def get_gpu_status(
    request: Request,
    _auth: dict[str, Any] = Depends(authenticate_admin),
):
    manager = getattr(request.app.state, "config_manager", None)
    deployment = manager.get("deployment", {}) if manager is not None else {}
    shared_gpu = bool(deployment.get("shared_gpu", False)) if isinstance(deployment, dict) else False
    gateway = await asyncio.to_thread(_gateway_cuda_status)
    comfy = await _probe(request)
    queue = comfy.get("queue") if isinstance(comfy, dict) else None
    queue_idle = None
    if isinstance(queue, dict):
        queue_idle = int(queue.get("running", 0) or 0) == 0 and int(queue.get("pending", 0) or 0) == 0
    comfy_memory = _comfy_memory(comfy.get("gpu") if isinstance(comfy, dict) else None)
    return {
        "data": {
            "gateway": gateway,
            "comfyui": {
                "available": bool(comfy.get("available")) if isinstance(comfy, dict) else False,
                "memory": comfy_memory,
                "endpoint_errors": comfy.get("endpoint_errors", {}) if isinstance(comfy, dict) else {},
            },
            "queue": queue,
            "queue_idle": queue_idle,
            "shared_gpu": shared_gpu,
            "diagnosis": _diagnosis(gateway, comfy_memory, queue_idle, shared_gpu),
        },
        "message": "success",
    }


async def _release_gateway_models() -> dict[str, bool]:
    from aigateway_core.prefix.cache.l3_semantic import release_l3_model

    l3_released = await asyncio.to_thread(release_l3_model)
    from . import admin_routes

    local_model = admin_routes._embedding_model_cache.pop("model", None)
    local_released = local_model is not None
    if local_model is not None:
        try:
            local_model.to("cpu")
        except Exception:
            pass
        del local_model
    await asyncio.to_thread(gc.collect)
    try:
        import torch

        if torch.cuda.is_initialized():
            torch.cuda.empty_cache()
    except ImportError:
        pass
    return {
        "l3_embedding": bool(l3_released),
        "rag_embedding": local_released,
    }


@router.post("/gpu/release")
async def release_gpu_memory(
    request: Request,
    _auth: dict[str, Any] = Depends(authenticate_admin),
):
    comfy = await _probe(request)
    queue = comfy.get("queue") if isinstance(comfy, dict) else None
    queue_idle = None
    if isinstance(queue, dict):
        running = int(queue.get("running", 0) or 0)
        pending = int(queue.get("pending", 0) or 0)
        queue_idle = running == 0 and pending == 0
        if not queue_idle:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "code": "gpu_busy",
                        "message": "GPU memory cannot be released while ComfyUI has active or pending work",
                    }
                },
            )

    released = await _release_gateway_models()
    comfy_release: dict[str, Any] = {"requested": False, "released": False}
    config = _comfy_config(request)
    server_url = str(config.get("server_url") or "").rstrip("/")
    comfy_available = bool(comfy.get("available")) if isinstance(comfy, dict) else False
    if server_url and comfy_available and queue_idle is True:
        comfy_release["requested"] = True
        try:
            timeout = httpx.Timeout(5.0, read=15.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{server_url}/free",
                    json={"unload_models": True, "free_memory": True},
                )
            response.raise_for_status()
            comfy_release["released"] = True
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            comfy_release["error"] = type(exc).__name__
    elif server_url and comfy_available and queue_idle is None:
        # Queue state is a safety precondition for unloading ComfyUI. Gateway
        # caches can still be released independently, but never disrupt an
        # unknown ComfyUI workload merely because /queue was unavailable.
        comfy_release["skipped"] = "queue_status_unknown"

    return {
        "data": {
            "gateway_models": released,
            "comfyui": comfy_release,
            "gateway": await asyncio.to_thread(_gateway_cuda_status),
        },
        "message": "success",
    }


def install_gpu_routes(admin_router: APIRouter) -> None:
    marker = "_aigateway_gpu_routes_installed"
    if getattr(admin_router, marker, False):
        return
    new_routes = list(router.routes)
    paths = {
        (getattr(route, "path", None), tuple(sorted(getattr(route, "methods", set()) or set())))
        for route in new_routes
    }
    admin_router.routes[:] = [
        route
        for route in admin_router.routes
        if (
            getattr(route, "path", None),
            tuple(sorted(getattr(route, "methods", set()) or set())),
        ) not in paths
    ]
    admin_router.routes[0:0] = new_routes
    setattr(admin_router, marker, True)


__all__ = ["install_gpu_routes", "router"]
