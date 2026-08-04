from __future__ import annotations

from unittest.mock import AsyncMock

import aigateway_api.source_draft_video_routes as routes_module
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_reloaded_draft_workflow_error_keeps_machine_code(monkeypatch):
    """A module reload must not turn a known domain rejection into HTTP 500."""

    async def authenticated():
        return {"user_id": "user-1", "group_id": "group-1"}

    reloaded_error_type = type("DraftWorkflowError", (Exception,), {})
    monkeypatch.setattr(
        routes_module,
        "create_video_draft_from_source",
        AsyncMock(side_effect=reloaded_error_type("video_duration_unsupported")),
    )

    app = FastAPI()
    app.state.draft_strategy = object()
    app.include_router(routes_module.router, prefix="/admin")
    app.dependency_overrides[routes_module.authenticate_admin] = authenticated

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/admin/draft/source-image/video",
            json={
                "motion_prompt": "move",
                "duration_seconds": 8,
                "fps": 8,
                "chat_session_id": "session-1",
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == (
        "video_duration_unsupported"
    )
