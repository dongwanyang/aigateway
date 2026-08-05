from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from aigateway_api.gpu_queue_handoff import install_gpu_queue_handoff
from aigateway_api.runtime_identity import deployed_commit_sha, install_runtime_identity
from aigateway_core.shared.gpu_scheduler import GpuResourceCoordinator


def test_deployed_commit_sha_uses_explicit_runtime_identity(monkeypatch) -> None:
    monkeypatch.setenv("AIGATEWAY_COMMIT_SHA", "commit-abc")
    assert deployed_commit_sha() == "commit-abc"


@pytest.mark.asyncio
async def test_health_wrapper_response_contains_runtime_identity(monkeypatch) -> None:
    monkeypatch.setenv("AIGATEWAY_COMMIT_SHA", "commit-abc")
    monkeypatch.setenv("AIGATEWAY_IMAGE_VERSION", "image-v2")
    router = APIRouter()

    @router.get("/health")
    async def health(request: Request) -> JSONResponse:
        assert request is not None
        return JSONResponse(content={"data": {"status": "healthy"}, "message": "success"})

    install_runtime_identity(router)
    install_runtime_identity(router)
    route = next(item for item in router.routes if item.path == "/health")
    response = await route.endpoint(SimpleNamespace())
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
