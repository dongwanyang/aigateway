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


def _scheduler_pairs(
    scheduler: dict[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return structurally executable worker/device pairs."""
    if not isinstance(scheduler, dict):
        return []
    devices = scheduler.get("devices") or []
    workers = scheduler.get("workers") or []
    if not isinstance(devices, list) or not isinstance(workers, list):
        return []
    devices_by_uuid = {
        str(device.get("uuid")): device
        for device in devices
        if isinstance(device, dict) and device.get("uuid")
    }
    return [
        (worker, devices_by_uuid[str(worker.get("device_uuid"))])
        for worker in workers
        if isinstance(worker, dict)
        and worker.get("device_uuid")
        and str(worker.get("device_uuid")) in devices_by_uuid
    ]


def _normalize_gateway_topology(
    gateway: dict[str, Any],
    *,
    comfy_available: bool,
    scheduler: dict[str, Any],
    pool_expected: bool = False,
) -> dict[str, Any]:
    """Normalize raw allocator facts against the shared GPU-pool topology.

    CUDA deployments expose the same physical devices to Gateway and one or more
    ComfyUI workers. ``GpuResourceCoordinator`` owns arbitration: Gateway may
    borrow an idle device, while generation drains Gateway leases and allocates
    the matching worker. ComfyUI availability therefore never turns an enabled
    but broken scheduler into a successful delegated topology.

    ``local_cuda_available`` preserves the raw nvidia-smi/PyTorch observation.
    ``available`` represents effective GPU availability after considering the
    scheduler inventory.
    """
    result = dict(gateway)
    enabled = bool(scheduler.get("enabled")) if isinstance(scheduler, dict) else False
    devices = scheduler.get("devices") if isinstance(scheduler, dict) else []
    workers = scheduler.get("workers") if isinstance(scheduler, dict) else []
    devices = devices if isinstance(devices, list) else []
    workers = workers if isinstance(workers, list) else []
    pairs = _scheduler_pairs(scheduler)
    local_available = bool(result.get("available"))

    result["local_cuda_available"] = local_available
    result["delegated_to"] = None
    result["scheduler_topology_complete"] = False

    # Shared-pool execution is authoritative whenever at least one configured
    # ComfyUI worker maps to an actually discovered physical device.
    if enabled and pairs:
        result["available"] = True
        result["status"] = "scheduler_pool"
        result["scheduler_topology_complete"] = True
        result["error"] = None
        return result

    # A CUDA deployment may legitimately use the scheduler only for Gateway
    # leases (for example Knowledge edition without local generation workers).
    if enabled and devices and not workers and not pool_expected:
        result["available"] = True
        result["status"] = "gateway_pool"
        result["scheduler_topology_complete"] = True
        result["error"] = None
        return result

    # Once the scheduler is enabled, missing inventory, orphan workers or an
    # expected pool with no worker/device pair is a topology fault. Do not hide
    # it behind a healthy ComfyUI HTTP probe: direct submission would bypass the
    # Coordinator's drain/fence/lock protocol.
    if enabled:
        result["available"] = False
        result["status"] = "scheduler_error"
        result["error"] = "gpu_scheduler_topology_incomplete"
        return result

    if local_available:
        result["status"] = "available"
        result["scheduler_topology_complete"] = True
        return result

    # Direct/external ComfyUI is a compatibility mode only when the local GPU
    # scheduler is explicitly disabled.
    if comfy_available:
        result["status"] = "delegated"
        result["delegated_to"] = "comfyui"
        if result.get("error") == "gpu_status_unavailable":
            result["error"] = None
        return result

    result["status"] = "unavailable"
    return result


def _execution_gpu_status(
    gateway: dict[str, Any],
    *,
    comfy_available: bool,
    normalized_comfy_memory: dict[str, Any] | None,
    scheduler: dict[str, Any],
    pool_expected: bool = False,
) -> dict[str, Any]:
    """Return the effective execution backend without losing pool semantics."""
    enabled = bool(scheduler.get("enabled")) if isinstance(scheduler, dict) else False
    devices = scheduler.get("devices") if isinstance(scheduler, dict) else []
    workers = scheduler.get("workers") if isinstance(scheduler, dict) else []
    devices = devices if isinstance(devices, list) else []
    workers = workers if isinstance(workers, list) else []
    pairs = _scheduler_pairs(scheduler)

    # Pool mode must win over the raw Gateway allocator flag. Both Gateway and
    # ComfyUI are clients of the Coordinator; neither owns the physical GPU.
    if enabled and pairs:
        paired_devices = {str(device.get("uuid")) for _, device in pairs}
        return {
            "available": True,
            "mode": "scheduler_pool",
            "owner": "scheduler",
            "device_count": len(paired_devices),
            "worker_count": len(pairs),
            "memory": None,
        }

    if enabled and devices and not workers and not pool_expected:
        return {
            "available": True,
            "mode": "gateway_pool",
            "owner": "scheduler",
            "device_count": len(devices),
            "worker_count": 0,
            "memory": None,
        }

    if enabled:
        return {
            "available": False,
            "mode": "scheduler_error",
            "owner": "scheduler",
            "device_count": len(devices),
            "worker_count": len(workers),
            "memory": None,
            "error": "gpu_scheduler_topology_incomplete",
        }

    if bool(gateway.get("local_cuda_available", gateway.get("available"))):
        return {
            "available": True,
            "mode": "gateway",
            "owner": "gateway",
            "memory": {
                "total_bytes": gateway.get("device_total_bytes"),
                "free_bytes": gateway.get("device_free_bytes"),
                "used_bytes": gateway.get("device_used_bytes"),
            },
        }

    if comfy_available:
        return {
            "available": True,
            "mode": "delegated_comfyui",
            "owner": "comfyui",
            "memory": normalized_comfy_memory,
        }
    return {
        "available": False,
        "mode": "unavailable",
        "owner": None,
        "memory": None,
        "error": gateway.get("error") or "gpu_status_unavailable",
    }


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
    comfy_available = (
        bool(comfy.get("available")) if isinstance(comfy, dict) else False
    )
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
    pool_expected = bool(scheduler.get("enabled")) and (
        shared_gpu or bool(scheduler.get("workers"))
    )
    gateway = _normalize_gateway_topology(
        gateway,
        comfy_available=comfy_available,
        scheduler=scheduler,
        pool_expected=pool_expected,
    )
    execution = _execution_gpu_status(
        gateway,
        comfy_available=comfy_available,
        normalized_comfy_memory=normalized_comfy_memory,
        scheduler=scheduler,
        pool_expected=pool_expected,
    )
    return {
        "data": {
            "gateway": gateway,
            "execution": execution,
            "comfyui": {
                "available": comfy_available,
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
            "pool_expected": pool_expected,
            "diagnosis": diagnose_memory(
                gateway,
                normalized_comfy_memory,
                idle,
                shared_gpu,
            ),
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
