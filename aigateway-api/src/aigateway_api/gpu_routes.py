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


def _scheduler_lists(
    scheduler: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(scheduler, dict):
        return [], []
    devices = scheduler.get("devices") or []
    workers = scheduler.get("workers") or []
    return (
        [item for item in devices if isinstance(item, dict)]
        if isinstance(devices, list)
        else [],
        [item for item in workers if isinstance(item, dict)]
        if isinstance(workers, list)
        else [],
    )


def _selector_allows(device_uuid: str, selector: Any) -> bool:
    if selector in (None, "auto"):
        return True
    return isinstance(selector, list) and device_uuid in {
        str(item) for item in selector
    }


def _device_override(scheduler: dict[str, Any], device_uuid: str) -> dict[str, Any]:
    overrides = scheduler.get("device_overrides") or []
    if not isinstance(overrides, list):
        return {}
    return next(
        (
            item
            for item in overrides
            if isinstance(item, dict) and str(item.get("uuid") or "") == device_uuid
        ),
        {},
    )


def _scheduler_pairs(
    scheduler: dict[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return worker/device UUID pairs without conflating runtime health."""
    devices, workers = _scheduler_lists(scheduler)
    devices_by_uuid = {
        str(device.get("uuid")): device
        for device in devices
        if device.get("uuid")
    }
    return [
        (worker, devices_by_uuid[str(worker.get("device_uuid"))])
        for worker in workers
        if worker.get("device_uuid")
        and str(worker.get("device_uuid")) in devices_by_uuid
    ]


def _scheduler_runnable_pairs(
    scheduler: dict[str, Any],
    *,
    capability: str | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return pairs currently eligible for generation scheduling.

    This mirrors the non-memory parts of ``GpuResourceCoordinator`` candidate
    selection: selector policy, device overrides, capability, health and worker
    cooldown/quarantine. Memory requirements are request-specific and therefore
    remain a generation-time decision.
    """
    runnable: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for worker, device in _scheduler_pairs(scheduler):
        device_uuid = str(device.get("uuid") or "")
        if not _selector_allows(device_uuid, scheduler.get("comfyui_devices")):
            continue
        override = _device_override(scheduler, device_uuid)
        if override.get("enabled") is False:
            continue
        capabilities = override.get("capabilities")
        if not isinstance(capabilities, list):
            worker_capabilities = worker.get("capabilities")
            capabilities = (
                worker_capabilities
                if isinstance(worker_capabilities, list)
                else ["image", "video", "upscale"]
            )
        if not capabilities:
            continue
        if capability is not None and capability not in capabilities:
            continue
        if worker.get("healthy") is False:
            continue
        if float(worker.get("unhealthy_cooldown_remaining_seconds", 0) or 0) > 0:
            continue
        if float(worker.get("oom_quarantine_remaining_seconds", 0) or 0) > 0:
            continue
        runnable.append((worker, device))
    return runnable


def _eligible_gateway_devices(scheduler: dict[str, Any]) -> list[dict[str, Any]]:
    devices, _ = _scheduler_lists(scheduler)
    result: list[dict[str, Any]] = []
    for device in devices:
        device_uuid = str(device.get("uuid") or "")
        if not device_uuid:
            continue
        if not _selector_allows(device_uuid, scheduler.get("gateway_devices")):
            continue
        if _device_override(scheduler, device_uuid).get("enabled") is False:
            continue
        result.append(device)
    return result


def _supported_capabilities(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    scheduler: dict[str, Any],
) -> list[str]:
    result: set[str] = set()
    for worker, device in pairs:
        override = _device_override(scheduler, str(device.get("uuid") or ""))
        capabilities = override.get("capabilities")
        if not isinstance(capabilities, list):
            worker_capabilities = worker.get("capabilities")
            capabilities = (
                worker_capabilities
                if isinstance(worker_capabilities, list)
                else ["image", "video", "upscale"]
            )
        result.update(str(item) for item in capabilities if item)
    return sorted(result)


def _normalize_gateway_topology(
    gateway: dict[str, Any],
    *,
    comfy_available: bool,
    scheduler: dict[str, Any],
    pool_expected: bool = False,
) -> dict[str, Any]:
    """Normalize raw CUDA visibility against the shared-pool contract."""
    result = dict(gateway)
    enabled = bool(scheduler.get("enabled")) if isinstance(scheduler, dict) else False
    devices, workers = _scheduler_lists(scheduler)
    pairs = _scheduler_pairs(scheduler)
    runnable_pairs = _scheduler_runnable_pairs(scheduler)
    local_available = bool(result.get("available"))

    result["local_cuda_available"] = local_available
    result["delegated_to"] = None
    result["scheduler_topology_complete"] = False
    result["scheduler_runnable"] = False

    if enabled and pairs:
        runnable = bool(runnable_pairs)
        result["available"] = runnable
        result["status"] = (
            "scheduler_pool" if runnable else "scheduler_pool_degraded"
        )
        result["scheduler_topology_complete"] = True
        result["scheduler_runnable"] = runnable
        result["error"] = None if runnable else "gpu_scheduler_no_runnable_worker"
        return result

    # A CUDA deployment can intentionally use the scheduler only for Gateway
    # components while generation runs on an external ComfyUI endpoint.
    if enabled and devices and not workers and not pool_expected:
        eligible_devices = _eligible_gateway_devices(scheduler)
        available = bool(eligible_devices)
        result["available"] = available
        result["status"] = "gateway_pool" if available else "gateway_pool_degraded"
        result["scheduler_topology_complete"] = True
        result["scheduler_runnable"] = available
        result["error"] = None if available else "gpu_scheduler_no_eligible_gateway_device"
        return result

    if enabled:
        result["available"] = False
        result["status"] = "scheduler_error"
        result["error"] = "gpu_scheduler_topology_incomplete"
        return result

    if local_available:
        result["status"] = "available"
        result["scheduler_topology_complete"] = True
        result["scheduler_runnable"] = True
        return result

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
    """Return effective ownership, topology and current runtime readiness."""
    enabled = bool(scheduler.get("enabled")) if isinstance(scheduler, dict) else False
    devices, workers = _scheduler_lists(scheduler)
    pairs = _scheduler_pairs(scheduler)
    runnable_pairs = _scheduler_runnable_pairs(scheduler)

    if enabled and pairs:
        paired_devices = {str(device.get("uuid")) for _, device in pairs}
        runnable_devices = {
            str(device.get("uuid")) for _, device in runnable_pairs
        }
        result = {
            "available": bool(runnable_pairs),
            "mode": "scheduler_pool",
            "owner": "scheduler",
            "topology_complete": True,
            "runnable_now": bool(runnable_pairs),
            "device_count": len(paired_devices),
            "worker_count": len(pairs),
            "runnable_device_count": len(runnable_devices),
            "runnable_worker_count": len(runnable_pairs),
            "supported_capabilities": _supported_capabilities(pairs, scheduler),
            "memory": None,
        }
        if not runnable_pairs:
            result["error"] = "gpu_scheduler_no_runnable_worker"
        return result

    if enabled and devices and not workers and not pool_expected:
        eligible_devices = _eligible_gateway_devices(scheduler)
        result = {
            "available": bool(eligible_devices),
            "mode": "gateway_pool",
            "owner": "scheduler",
            "topology_complete": True,
            "runnable_now": bool(eligible_devices),
            "device_count": len(devices),
            "worker_count": 0,
            "external_comfyui_available": comfy_available,
            "memory": None,
        }
        if not eligible_devices:
            result["error"] = "gpu_scheduler_no_eligible_gateway_device"
        return result

    if enabled:
        return {
            "available": False,
            "mode": "scheduler_error",
            "owner": "scheduler",
            "topology_complete": False,
            "runnable_now": False,
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
            "topology_complete": True,
            "runnable_now": True,
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
            "topology_complete": True,
            "runnable_now": True,
            "memory": normalized_comfy_memory,
        }
    return {
        "available": False,
        "mode": "unavailable",
        "owner": None,
        "topology_complete": False,
        "runnable_now": False,
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
    scheduler_config = (
        manager.get("gpu_scheduler", {}) if manager is not None else {}
    )
    comfy_config = _comfy_config(request)
    if not isinstance(scheduler_config, dict):
        scheduler_config = {}
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
        "enabled": bool(scheduler_config.get("enabled", False)),
        "policy": "unavailable",
        "generation_priority": False,
        "generation_queue_depth": 0,
        "devices": [],
        "workers": [],
    }
    # Runtime status owns mutable telemetry; persisted configuration owns policy.
    for key in (
        "gateway_devices",
        "comfyui_devices",
        "device_overrides",
        "device_safety_margin_gb",
    ):
        if key in scheduler_config:
            scheduler[key] = scheduler_config[key]
    scheduler_managed = bool(comfy_config.get("scheduler_managed", False))
    pool_expected = bool(scheduler.get("enabled")) and (
        scheduler_managed or shared_gpu or bool(scheduler.get("workers"))
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
            "scheduler_managed": scheduler_managed,
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
