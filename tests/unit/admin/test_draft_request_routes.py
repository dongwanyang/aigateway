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
from aigateway_api.generation_request_state import (
    REQUEST_RECORD_FAILED,
    REQUEST_RECORD_NON_DRAFT,
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


def _record(draft_id: str) -> dict[str, object | None]:
    return {
        "draft_id": draft_id,
        "user_id": "user-1",
        "group_id": None,
        "session_id": "session-1",
    }


@pytest.mark.asyncio
async def test_get_request_returns_unregistered_before_index_exists() -> None:
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
    assert json.loads(response.body)["status"] == "unregistered"


@pytest.mark.asyncio
async def test_get_request_returns_resolving_after_pending_index_exists() -> None:
    strategy = SimpleNamespace(
        resolve_request=AsyncMock(return_value=(None, _record("")))
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
    strategy = SimpleNamespace(
        resolve_request=AsyncMock(return_value=(draft, _record(draft.draft_id)))
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
async def test_get_request_returns_non_draft_terminal_state() -> None:
    strategy = SimpleNamespace(
        resolve_request=AsyncMock(
            return_value=(None, _record(REQUEST_RECORD_NON_DRAFT))
        )
    )

    result = await get_generation_request(
        "request-text",
        _request(strategy),
        chat_session_id="session-1",
        auth={"user_id": "user-1", "group_id": None},
    )

    assert result == {
        "request_id": "request-text",
        "status": "non_draft",
    }


@pytest.mark.asyncio
async def test_get_request_returns_failed_terminal_state() -> None:
    strategy = SimpleNamespace(
        resolve_request=AsyncMock(
            return_value=(None, _record(REQUEST_RECORD_FAILED))
        )
    )

    result = await get_generation_request(
        "request-failed",
        _request(strategy),
        chat_session_id="session-1",
        auth={"user_id": "user-1", "group_id": None},
    )

    assert result == {
        "request_id": "request-failed",
        "status": "failed",
        "error": "generation_request_failed",
    }


@pytest.mark.asyncio
async def test_get_request_rejects_wrong_session_before_disclosing_draft() -> None:
    strategy = SimpleNamespace(
        resolve_request=AsyncMock(return_value=(
            _draft(),
            _record("draft-1"),
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
async def test_cancel_non_draft_request_completes_transport_stop() -> None:
    strategy = SimpleNamespace(
        resolve_request=AsyncMock(
            return_value=(None, _record(REQUEST_RECORD_NON_DRAFT))
        ),
        cancel_request=AsyncMock(),
    )

    result = await cancel_generation_request(
        "request-text",
        _request(strategy),
        chat_session_id="session-1",
        auth={"user_id": "user-1", "group_id": None},
    )

    assert result == {
        "request_id": "request-text",
        "status": "cancelled",
        "stage": "transport_cancelled",
    }
    strategy.cancel_request.assert_not_awaited()


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
