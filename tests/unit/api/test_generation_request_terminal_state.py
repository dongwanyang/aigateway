from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aigateway_api.generation_request_state import (
    REQUEST_RECORD_FAILED,
    REQUEST_RECORD_NON_DRAFT,
    terminal_request_status,
)
from aigateway_api.video_request_guard import _mark_request_terminal


def _request(strategy: object) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(
            request_id="request-1",
            draft_owner={"user_id": "user-1", "group_id": None},
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(draft_strategy=strategy),
        ),
    )


def _body() -> SimpleNamespace:
    return SimpleNamespace(chat_session_id="session-1")


@pytest.mark.asyncio
async def test_terminal_record_replaces_only_pending_request() -> None:
    pending = {
        "draft_id": "",
        "user_id": "user-1",
        "group_id": None,
        "session_id": "session-1",
    }
    strategy = SimpleNamespace(
        resolve_request=AsyncMock(return_value=(None, pending)),
        register_request_draft=AsyncMock(),
    )

    await _mark_request_terminal(
        _body(),
        _request(strategy),
        REQUEST_RECORD_NON_DRAFT,
    )

    strategy.register_request_draft.assert_awaited_once_with(
        "request-1",
        REQUEST_RECORD_NON_DRAFT,
        user_id="user-1",
        group_id=None,
        session_id="session-1",
        ttl_seconds=300,
    )


@pytest.mark.asyncio
async def test_terminal_record_never_overwrites_real_draft() -> None:
    draft = SimpleNamespace(draft_id="draft-1")
    record = {
        "draft_id": "draft-1",
        "user_id": "user-1",
        "group_id": None,
        "session_id": "session-1",
    }
    strategy = SimpleNamespace(
        resolve_request=AsyncMock(return_value=(draft, record)),
        register_request_draft=AsyncMock(),
    )

    await _mark_request_terminal(
        _body(),
        _request(strategy),
        REQUEST_RECORD_FAILED,
    )

    strategy.register_request_draft.assert_not_awaited()


def test_terminal_markers_have_explicit_public_statuses() -> None:
    assert terminal_request_status(
        {"draft_id": REQUEST_RECORD_NON_DRAFT}
    ) == "non_draft"
    assert terminal_request_status(
        {"draft_id": REQUEST_RECORD_FAILED}
    ) == "failed"
    assert terminal_request_status({"draft_id": "draft-1"}) is None
