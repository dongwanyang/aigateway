from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aigateway_api.source_draft_video_routes as routes_module
import pytest
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
            "request_id": "request-1",
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


def _request_body() -> dict[str, object]:
    return {
        "motion_prompt": "move",
        "duration_seconds": 5,
        "fps": 8,
        "chat_session_id": "session-1",
    }


def test_source_video_route_is_installed_in_fresh_process():
    api_src = Path(routes_module.__file__).resolve().parents[1]
    repo_root = api_src.parents[1]
    core_src = repo_root / "aigateway-core" / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(api_src),
            str(core_src),
            env.get("PYTHONPATH", ""),
        ]
    )
    code = """
import aigateway_api
from aigateway_api import admin_routes
paths = {getattr(route, 'path', '') for route in admin_routes.router.routes}
assert '/draft/{source_draft_id}/video' in paths, (aigateway_api.__file__, sorted(paths))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr or result.stdout


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
            headers={"X-Request-ID": "request-1"},
            json={
                "motion_prompt": "柯基跑向镜头",
                "duration_seconds": 5,
                "fps": 8,
                "chat_session_id": "session-1",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "request_id": "request-1",
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
        trace_id="request-1",
        request_id="request-1",
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
            json=_request_body(),
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
            json=_request_body(),
        )

    assert response.status_code == status_code
    assert response.json()["detail"]["error"]["code"] == code


@pytest.mark.asyncio
async def test_source_video_route_maps_unexpected_storage_failure_to_500(
    monkeypatch,
):
    async def authenticated():
        return {"user_id": "user-1", "group_id": "group-1"}

    app = _app(authenticated)
    monkeypatch.setattr(
        routes_module,
        "create_video_draft_from_source",
        AsyncMock(side_effect=OSError("permission denied")),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/admin/draft/source-image/video",
            json=_request_body(),
        )

    assert response.status_code == 500
    assert response.json()["detail"]["error"] == {
        "code": "internal_error",
        "message": "创建视频草稿时发生内部错误。",
    }


@pytest.mark.asyncio
async def test_source_video_creation_is_recorded_in_request_logs(monkeypatch):
    """图生视频草稿创建必须写请求日志。

    回归:这条路由会触发本地 ComfyUI 关键帧作业,但之前完全不调用
    _record_request_log,图生视频的这段消耗在 Logs 页不可见。
    """
    from aigateway_api import openai_compat

    async def authenticated():
        return {"user_id": "user-1", "group_id": "group-1"}

    draft = _draft_response()
    draft.workflow_version = "wan2.2-ti2v-5b-v1"
    app = _app(authenticated)
    monkeypatch.setattr(
        routes_module, "create_video_draft_from_source", AsyncMock(return_value=draft)
    )
    record_log = AsyncMock()
    monkeypatch.setattr(openai_compat, "_record_request_log", record_log)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/admin/draft/source-image/video", json=_request_body()
        )

    assert response.status_code == 200
    record_log.assert_awaited_once()
    logged = record_log.await_args.kwargs
    assert logged["endpoint"] == "/admin/draft/source-image/video"
    assert logged["status_code"] == 200
    assert logged["method"] == "POST"
    # 模型标识来自草稿的真实工作流,不写死。
    assert logged["model"] == "comfyui:video:wan2.2-ti2v-5b-v1"


@pytest.mark.asyncio
async def test_source_video_domain_failure_is_recorded_in_request_logs(monkeypatch):
    """被拒绝的图生视频请求也要留下日志，否则失败原因在 Logs 页无迹可寻。"""
    from aigateway_api import openai_compat

    async def authenticated():
        return {"user_id": "user-1", "group_id": "group-1"}

    app = _app(authenticated)
    monkeypatch.setattr(
        routes_module,
        "create_video_draft_from_source",
        AsyncMock(side_effect=DraftWorkflowError("source_draft_forbidden")),
    )
    record_log = AsyncMock()
    monkeypatch.setattr(openai_compat, "_record_request_log", record_log)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/admin/draft/source-image/video", json=_request_body()
        )

    assert response.status_code == 403
    record_log.assert_awaited_once()
    assert record_log.await_args.kwargs["status_code"] == 403


@pytest.mark.asyncio
async def test_source_video_request_log_failure_does_not_break_creation(monkeypatch):
    """日志后端故障不能影响草稿创建结果。"""
    from aigateway_api import openai_compat

    async def authenticated():
        return {"user_id": "user-1", "group_id": "group-1"}

    app = _app(authenticated)
    monkeypatch.setattr(
        routes_module,
        "create_video_draft_from_source",
        AsyncMock(return_value=_draft_response()),
    )
    monkeypatch.setattr(
        openai_compat,
        "_record_request_log",
        AsyncMock(side_effect=RuntimeError("log store down")),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/admin/draft/source-image/video", json=_request_body()
        )

    assert response.status_code == 200
    assert response.json()["draft_id"] == "video-draft"
