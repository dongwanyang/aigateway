from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from aigateway_api.draft_request_routes import (
    cancel_generation_request,
    get_generation_request,
)
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError


def _request(strategy):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(draft_strategy=strategy)
        )
    )


def _draft(**overrides):
    values = {
        "draft_id": "draft-1",
        "status": "running",
        "stage": "running",
        "progress": 0.2,
        "media_type": "image",
        "expires_at": 1234.0,
        "workflow_version": "image-v1",
        "error": None,
        "user_id": "user-1",
        "group_id": None,
        "session_id": "session-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_get_request_returns_202_until_draft_index_exists() -> None:
    strategy = SimpleNamespace(
        resolve_request=AsyncMock(return_value=(None, None))
    )

    response = await get_generation_request(
        "request-1",
        _request(strategy),
        chat_session_id="session-1",
        auth={"user_id": "user-1", "group_id": None},
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 202
    assert json.loads(response.body)["status"] == "resolving"


@pytest.mark.asyncio
async def test_get_request_returns_owned_draft_payload() -> None:
    draft = _draft()
    record = {
        "draft_id": draft.draft_id,
        "user_id": "user-1",
        "group_id": None,
        "session_id": "session-1",
    }
    strategy = SimpleNamespace(
        resolve_request=AsyncMock(return_value=(draft, record))
    )

    result = await get_generation_request(
        "request-1",
        _request(strategy),
        chat_session_id="session-1",
        auth={"user_id": "user-1", "group_id": None},
    )

    assert result["draft_id"] == "draft-1"
    assert result["preview_url"] == "/admin/draft/draft-1/preview"
    assert result["status"] == "running"


@pytest.mark.asyncio
async def test_get_request_rejects_wrong_session_before_disclosing_draft() -> None:
    strategy = SimpleNamespace(
        resolve_request=AsyncMock(return_value=(
            _draft(),
            {
                "draft_id": "draft-1",
                "user_id": "user-1",
                "group_id": None,
                "session_id": "session-1",
            },
        ))
    )

    with pytest.raises(HTTPException) as raised:
        await get_generation_request(
            "request-1",
            _request(strategy),
            chat_session_id="session-2",
            auth={"user_id": "user-1", "group_id": None},
        )

    assert raised.value.status_code == 403
    assert raised.value.detail["error"]["code"] == "generation_request_forbidden"


@pytest.mark.asyncio
async def test_cancel_request_returns_202_for_pre_registration_tombstone() -> None:
    strategy = SimpleNamespace(
        cancel_request=AsyncMock(return_value=None)
    )

    response = await cancel_generation_request(
        "request-1",
        _request(strategy),
        chat_session_id="session-1",
        auth={"user_id": "user-1", "group_id": None},
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 202
    assert json.loads(response.body)["status"] == "cancellation_requested"
    strategy.cancel_request.assert_awaited_once_with(
        "request-1",
        user_id="user-1",
        group_id=None,
        session_id="session-1",
    )


@pytest.mark.asyncio
async def test_cancel_request_preserves_forbidden_error() -> None:
    strategy = SimpleNamespace(
        cancel_request=AsyncMock(
            side_effect=DraftWorkflowError("generation_request_forbidden")
        )
    )

    with pytest.raises(HTTPException) as raised:
        await cancel_generation_request(
            "request-1",
            _request(strategy),
            chat_session_id="session-1",
            auth={"user_id": "user-2", "group_id": None},
        )

    assert raised.value.status_code == 403
    assert raised.value.detail["error"]["code"] == "generation_request_forbidden"
