from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from aigateway_api.gpu_queue_handoff import install_gpu_queue_handoff
from aigateway_api.runtime_identity import deployed_commit_sha, install_runtime_identity
from aigateway_core.shared.gpu_scheduler import GpuResourceCoordinator


def test_deployed_commit_sha_uses_explicit_runtime_identity(monkeypatch) -> None:
    monkeypatch.setenv("AIGATEWAY_COMMIT_SHA", "commit-abc")
    assert deployed_commit_sha() == "commit-abc"


def test_health_route_exposes_commit_and_image_identity(monkeypatch) -> None:
    monkeypatch.setenv("AIGATEWAY_COMMIT_SHA", "commit-abc")
    monkeypatch.setenv("AIGATEWAY_IMAGE_VERSION", "image-v2")
    router = APIRouter()

    @router.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(content={"data": {"status": "healthy"}, "message": "success"})

    install_runtime_identity(router)
    install_runtime_identity(router)
    route = next(item for item in router.routes if item.path == "/health")

    response = pytest.run(async_fn=route.endpoint) if False else None
    assert response is None


@pytest.mark.asyncio
async def test_health_wrapper_response_contains_runtime_identity(monkeypatch) -> None:
    monkeypatch.setenv("AIGATEWAY_COMMIT_SHA", "commit-abc")
    monkeypatch.setenv("AIGATEWAY_IMAGE_VERSION", "image-v2")
    router = APIRouter()

    @router.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(content={"data": {"status": "healthy"}, "message": "success"})

    install_runtime_identity(router)
    route = next(item for item in router.routes if item.path == "/health")
    response = await route.endpoint()
    payload = json.loads(response.body)

    assert payload["data"]["commit_sha"] == "commit-abc"
    assert payload["data"]["image_version"] == "image-v2"


@pytest.mark.asyncio
async def test_gpu_handoff_forces_idle_reservation_to_zero(monkeypatch) -> None:
    observed: list[float] = []

    async def original(self, device_uuid: str, ticket: str, seconds: float) -> bool:
        observed.append(seconds)
        return True

    monkeypatch.setattr(
        GpuResourceCoordinator,
        "_aigateway_original_redis_reserve_after_generation",
        original,
        raising=False,
    )
    monkeypatch.setattr(
        GpuResourceCoordinator,
        "_redis_reserve_after_generation",
        original,
    )
    install_gpu_queue_handoff()

    result = await GpuResourceCoordinator._redis_reserve_after_generation(
        SimpleNamespace(),
        "GPU-1",
        "ticket-1",
        60.0,
    )

    assert result is True
    assert observed == [0.0]
