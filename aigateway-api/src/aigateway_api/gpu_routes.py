"""Authenticated GPU diagnostics and idle-memory release routes."""
from __future__ import annotations

import asyncio
import gc
from typing import Any

import httpx
from aigateway_core.shared.gpu_memory import (
    comfy_memory,
    diagnose_memory,
    gateway_cuda_status,
)
from aigateway_core.shared.gpu_scheduler import (
    GpuSchedulerConfig,
    GpuSchedulerConfigError,
)
from fastapi import APIRouter, Depends, HTTPException, Request

from .auth_middleware import authenticate_admin
from .config_security import read_versioned_yaml_config, transactional_replace_config
from .security_routes import (
    _commit_revision,
    _config_error,
    _expected_revision,
    _json_object,
    _manager,
    _versioned_response,
)

router = APIRouter()


def _comfy_config(request: Request) -> dict[str, Any]:
    manager = getattr(request.app.state, "config_manager", None)
    value = (
        manager.get("generation_optimization.draft_workflow.comfyui", {})
        if manager is not None
        else {}
    )
    return value if isinstance(value, dict) else {}


async def _probe(request: Request) -> dict[str, Any]:
    from .local_generation import probe_comfyui

    return await probe_comfyui(_comfy_config(request))


def _queue_idle(queue: Any) -> bool | None:
    if not isinstance(queue, dict):
        return None
    return (
        int(queue.get("running", 0) or 0) == 0
        and int(queue.get("pending", 0) or 0) == 0
    )


@router.get("/gpu/status")
async def get_gpu_status(
    request: Request,
    _auth: dict[str, Any] = Depends(authenticate_admin),
):
    manager = getattr(request.app.state, "config_manager", None)
    deployment = manager.get("deployment", {}) if manager is not None else {}
    shared_gpu = (
        bool(deployment.get("shared_gpu", False))
        if isinstance(deployment, dict)
        else False
    )
    gateway = await asyncio.to_thread(gateway_cuda_status)
    comfy = await _probe(request)
    queue = comfy.get("queue") if isinstance(comfy, dict) else None
    idle = _queue_idle(queue)
    normalized_comfy_memory = comfy_memory(
        comfy.get("gpu") if isinstance(comfy, dict) else None
    )
    coordinator = getattr(request.app.state, "gpu_coordinator", None)
    if coordinator is not None:
        from aigateway_core.shared.gpu_scheduler import discover_nvidia_devices

        coordinator.refresh_inventory(
            await asyncio.to_thread(discover_nvidia_devices)
        )
    scheduler = coordinator.status() if coordinator is not None else {
        "enabled": False,
        "policy": "unavailable",
        "generation_priority": False,
        "generation_queue_depth": 0,
        "devices": [],
        "workers": [],
    }
    return {
        "data": {
            "gateway": gateway,
            "comfyui": {
                "available": (
                    bool(comfy.get("available")) if isinstance(comfy, dict) else False
                ),
                "memory": normalized_comfy_memory,
                "endpoint_errors": (
                    comfy.get("endpoint_errors", {})
                    if isinstance(comfy, dict)
                    else {}
                ),
            },
            "queue": queue,
            "queue_idle": idle,
            "shared_gpu": shared_gpu,
            "diagnosis": diagnose_memory(
                gateway,
                normalized_comfy_memory,
                idle,
                shared_gpu,
            ),
            # New physical-device/worker view. Legacy gateway/comfyui/queue
            # fields above remain available for one compatibility release.
            "scheduler": scheduler,
            "devices": scheduler["devices"],
            "workers": scheduler["workers"],
        },
        "message": "success",
    }


_GPU_CONFIG_FIELDS = {
    "enabled",
    "policy",
    "generation_priority",
    "gateway_devices",
    "comfyui_devices",
    "gateway_fallback",
    "generation_wait_timeout_seconds",
    "comfyui_idle_reservation_seconds",
    "lease_ttl_seconds",
    "lease_heartbeat_seconds",
    "worker_probe_interval_seconds",
    "worker_unhealthy_cooldown_seconds",
    "oom_quarantine_seconds",
    "max_worker_failover_attempts",
    "device_safety_margin_gb",
    "gateway_memory_limit_percent",
    "device_overrides",
    "comfyui_dynamic_vram_enabled",
    "topology_auto_apply",
    "topology_reconcile_interval_seconds",
}
_GPU_RESTART_FIELDS = {
    "gateway_devices",
    "comfyui_devices",
    "device_overrides",
    "comfyui_dynamic_vram_enabled",
}


@router.put("/gpu/config")
async def update_gpu_config(
    request: Request,
    _auth: dict[str, Any] = Depends(authenticate_admin),
):
    """Atomically update GPU policy and classify hot/restart-only fields."""
    manager = _manager(request)
    try:
        expected_revision = _expected_revision(request)
        submitted = await _json_object(request)
        unknown = sorted(set(submitted) - _GPU_CONFIG_FIELDS)
        if unknown:
            raise HTTPException(
                422,
                detail={
                    "error": {
                        "code": "validation_error",
                        "message": "Unknown GPU scheduler fields: " + ", ".join(unknown),
                    }
                },
            )
        candidate, _ = read_versioned_yaml_config(manager.config_path)
        current = candidate.get("gpu_scheduler", {})
        if not isinstance(current, dict):
            current = {}
        merged = {**current, **submitted}
        normalized = GpuSchedulerConfig.from_mapping(merged).public_dict()
        # Worker topology is generated by the installer and is not writable
        # through this policy endpoint.
        if "workers" in current:
            normalized["workers"] = current["workers"]
        changed = [key for key in submitted if current.get(key) != normalized.get(key)]
        applied_fields = sorted(set(changed) - _GPU_RESTART_FIELDS)
        restart_required_fields = sorted(set(changed) & _GPU_RESTART_FIELDS)
        candidate["gpu_scheduler"] = normalized
        commit = transactional_replace_config(
            manager.config_path,
            candidate,
            manager,
            expected_revision=expected_revision,
        )
    except GpuSchedulerConfigError as exc:
        raise HTTPException(
            422,
            detail={"error": {"code": "validation_error", "message": str(exc)}},
        ) from exc
    except Exception as exc:
        raise _config_error(exc) from exc

    return _versioned_response(
        {
            "config": normalized,
            "applied_fields": applied_fields,
            "restart_required_fields": restart_required_fields,
            "restart_required": bool(restart_required_fields),
        },
        _commit_revision(commit, manager.config_path),
    )


async def _release_gateway_models() -> dict[str, bool]:
    from aigateway_core.prefix.cache.l3_semantic import release_l3_model

    from .embedding_model_runtime import embedding_model_runtime

    rag_release = await asyncio.to_thread(
        embedding_model_runtime.release_if_idle
    )
    if rag_release["busy"]:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "gateway_gpu_busy",
                    "message": (
                        "Gateway embedding memory cannot be released while "
                        "local embedding inference is active"
                    ),
                }
            },
        )

    l3_released = await asyncio.to_thread(release_l3_model)
    await asyncio.to_thread(gc.collect)
    try:
        import torch

        if torch.cuda.is_initialized():
            torch.cuda.empty_cache()
    except ImportError:
        pass
    return {
        "l3_embedding": bool(l3_released),
        "rag_embedding": bool(rag_release["released"]),
    }


async def _release_comfyui(server_url: str) -> dict[str, Any]:
    result: dict[str, Any] = {"requested": True, "released": False}
    try:
        timeout = httpx.Timeout(5.0, read=15.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{server_url}/free",
                json={"unload_models": True, "free_memory": True},
            )
        response.raise_for_status()
        result["released"] = True
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        result["error"] = type(exc).__name__
    return result


@router.post("/gpu/release")
async def release_gpu_memory(
    request: Request,
    _auth: dict[str, Any] = Depends(authenticate_admin),
):
    comfy = await _probe(request)
    queue = comfy.get("queue") if isinstance(comfy, dict) else None
    idle = _queue_idle(queue)
    if idle is False:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "gpu_busy",
                    "message": (
                        "GPU memory cannot be released while ComfyUI has "
                        "active or pending work"
                    ),
                }
            },
        )

    released = await _release_gateway_models()
    comfy_release: dict[str, Any] = {"requested": False, "released": False}
    coordinator = getattr(request.app.state, "gpu_coordinator", None)
    if coordinator is not None and coordinator.status().get("workers"):
        worker_results = await coordinator.release_idle_workers_now()
        comfy_release = {
            "requested": True,
            "released": bool(worker_results) and all(worker_results.values()),
            "workers": worker_results,
        }
    config = _comfy_config(request)
    server_url = str(config.get("server_url") or "").rstrip("/")
    comfy_available = (
        bool(comfy.get("available")) if isinstance(comfy, dict) else False
    )
    if (
        not comfy_release["requested"]
        and server_url
        and comfy_available
        and idle is True
    ):
        comfy_release = await _release_comfyui(server_url)
    elif server_url and comfy_available and idle is None:
        comfy_release["skipped"] = "queue_status_unknown"

    return {
        "data": {
            "gateway_models": released,
            "comfyui": comfy_release,
            "gateway": await asyncio.to_thread(gateway_cuda_status),
        },
        "message": "success",
    }


def install_gpu_routes(admin_router: APIRouter) -> None:
    marker = "_aigateway_gpu_routes_installed"
    if getattr(admin_router, marker, False):
        return
    new_routes = list(router.routes)
    paths = {
        (
            getattr(route, "path", None),
            tuple(sorted(getattr(route, "methods", set()) or set())),
        )
        for route in new_routes
    }
    admin_router.routes[:] = [
        route
        for route in admin_router.routes
        if (
            getattr(route, "path", None),
            tuple(sorted(getattr(route, "methods", set()) or set())),
        )
        not in paths
    ]
    admin_router.routes[0:0] = new_routes
    setattr(admin_router, marker, True)


__all__ = ["install_gpu_routes", "router"]
