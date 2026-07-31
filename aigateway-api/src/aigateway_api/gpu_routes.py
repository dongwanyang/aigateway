"""Authenticated GPU diagnostics and idle-memory release routes."""
from __future__ import annotations

import asyncio
import gc
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from aigateway_core.shared.gpu_memory import (
    comfy_memory,
    diagnose_memory,
    gateway_cuda_status,
)

from .auth_middleware import authenticate_admin

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
    config = _comfy_config(request)
    server_url = str(config.get("server_url") or "").rstrip("/")
    comfy_available = (
        bool(comfy.get("available")) if isinstance(comfy, dict) else False
    )
    if server_url and comfy_available and idle is True:
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
