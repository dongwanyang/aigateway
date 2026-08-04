from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import aigateway_api.source_draft_video_routes as routes_module
import pytest
from aigateway_api import admin_routes
from aigateway_core.pipelines.generation._common.exceptions import (
    DraftWorkflowError,
)
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient


def _draft_response():
    return SimpleNamespace(
        draft_id="video-draft",
        status="pending",
        expires_at=1234.0,
        generation_params={
            "source_image_sha256": "abc123",
            "duration_seconds": 5.0,
            "fps": 8,
            "frame_count": 41,
        },
    )


def _app(auth_dependency) -> FastAPI:
    app = FastAPI()
    app.state.draft_strategy = object()
    app.include_router(routes_module.router, prefix="/admin")
    app.dependency_overrides[routes_module.authenticate_admin] = auth_dependency
    return app


@pytest.mark.asyncio
async def test_source_video_route_is_installed_on_admin_router():
    paths = {
        getattr(route, "path", "")
        for route in admin_routes.router.routes
    }
    assert "/draft/{source_draft_id}/video" in paths


@pytest.mark.asyncio
async def test_source_video_route_forwards_authenticated_owner(monkeypatch):
    async def authenticated():
        return {"user_id": "user-1", "group_id": "group-1"}

    app = _app(authenticated)
    create = AsyncMock(return_value=_draft_response())
    monkeypatch.setattr(routes_module, "create_video_draft_from_source", create)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/admin/draft/source-image/video",
            json={
                "motion_prompt": "柯基跑向镜头",
                "duration_seconds": 5,
                "fps": 8,
                "chat_session_id": "session-1",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "source_draft_id": "source-image",
        "draft_id": "video-draft",
        "status": "pending",
        "media_type": "video",
        "preview_url": "/admin/draft/video-draft/preview",
        "source_image_sha256": "abc123",
        "duration_seconds": 5.0,
        "fps": 8,
        "frame_count": 41,
        "expires_at": 1234.0,
    }
    create.assert_awaited_once_with(
        app.state.draft_strategy,
        source_draft_id="source-image",
        motion_prompt="柯基跑向镜头",
        duration_seconds=5,
        fps=8,
        chat_session_id="session-1",
        user_id="user-1",
        group_id="group-1",
        trace_id="",
    )


@pytest.mark.asyncio
async def test_source_video_route_requires_admin_authentication(monkeypatch):
    async def denied():
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "unauthorized", "message": "login"}},
        )

    app = _app(denied)
    create = AsyncMock()
    monkeypatch.setattr(routes_module, "create_video_draft_from_source", create)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/admin/draft/source-image/video",
            json={
                "motion_prompt": "move",
                "duration_seconds": 5,
                "fps": 8,
                "chat_session_id": "session-1",
            },
        )

    assert response.status_code == 401
    create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        ("source_draft_forbidden", 403, "source_draft_forbidden"),
        ("source_draft_invalid_type", 409, "source_draft_invalid_type"),
        (
            "comfyui_missing_dependencies: diffusion_models/wan.safetensors",
            503,
            "comfyui_missing_dependencies",
        ),
    ],
)
async def test_source_video_route_maps_domain_errors(
    monkeypatch,
    error,
    status_code,
    code,
):
    async def authenticated():
        return {"user_id": "user-1", "group_id": "group-1"}

    app = _app(authenticated)
    monkeypatch.setattr(
        routes_module,
        "create_video_draft_from_source",
        AsyncMock(side_effect=DraftWorkflowError(error)),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/admin/draft/source-image/video",
            json={
                "motion_prompt": "move",
                "duration_seconds": 5,
                "fps": 8,
                "chat_session_id": "session-1",
            },
        )

    assert response.status_code == status_code
    assert response.json()["detail"]["error"]["code"] == code
