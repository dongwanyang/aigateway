from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aigateway_api.draft_rejection_lifecycle import (
    reject_draft_with_request_handoff,
)
from aigateway_api.generation_request_state import REQUEST_RECORD_FAILED


def _draft(draft_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        draft_id=draft_id,
        generation_params={"request_id": "request-1"},
        user_id="user-1",
        group_id=None,
        session_id="session-1",
        expires_at=time.time() + 3600,
    )


def _strategy(old_draft: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        get_draft=AsyncMock(return_value=old_draft),
        register_request_draft=AsyncMock(),
        _cancel_record=AsyncMock(return_value=None),
        _record_matches_owner=lambda *_args, **_kwargs: True,
        cancel_draft=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_rejection_moves_request_mapping_to_new_draft() -> None:
    old_draft = _draft("draft-old")
    new_draft = _draft("draft-new")
    strategy = _strategy(old_draft)
    original = AsyncMock(return_value=new_draft)

    result = await reject_draft_with_request_handoff(
        strategy,
        old_draft.draft_id,
        original,
    )

    assert result is new_draft
    original.assert_awaited_once_with(strategy, old_draft.draft_id)
    assert strategy.register_request_draft.await_count == 2
    first = strategy.register_request_draft.await_args_list[0]
    second = strategy.register_request_draft.await_args_list[1]
    assert first.args[:2] == ("request-1", "")
    assert second.args[:2] == ("request-1", "draft-new")
    for call in (first, second):
        assert call.kwargs["user_id"] == "user-1"
        assert call.kwargs["session_id"] == "session-1"
        assert call.kwargs["ttl_seconds"] > 0
    strategy.cancel_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_stop_cancels_regenerated_background_task() -> None:
    old_draft = _draft("draft-old")
    new_draft = _draft("draft-new")
    cancelled = _draft("draft-new")
    strategy = _strategy(old_draft)
    strategy._cancel_record.return_value = {
        "user_id": "user-1",
        "group_id": None,
        "session_id": "session-1",
    }
    strategy.cancel_draft.return_value = cancelled
    original = AsyncMock(return_value=new_draft)

    result = await reject_draft_with_request_handoff(
        strategy,
        old_draft.draft_id,
        original,
    )

    assert result is cancelled
    strategy.cancel_draft.assert_awaited_once_with("draft-new")


@pytest.mark.asyncio
async def test_rejection_failure_restores_surviving_old_mapping() -> None:
    old_draft = _draft("draft-old")
    strategy = _strategy(old_draft)
    strategy.get_draft = AsyncMock(side_effect=[old_draft, old_draft])
    original = AsyncMock(side_effect=RuntimeError("regeneration failed"))

    with pytest.raises(RuntimeError, match="regeneration failed"):
        await reject_draft_with_request_handoff(
            strategy,
            old_draft.draft_id,
            original,
        )

    assert strategy.register_request_draft.await_count == 2
    assert strategy.register_request_draft.await_args_list[1].args[:2] == (
        "request-1",
        "draft-old",
    )


@pytest.mark.asyncio
async def test_rejection_failure_marks_request_failed_after_old_delete() -> None:
    old_draft = _draft("draft-old")
    strategy = _strategy(old_draft)
    strategy.get_draft = AsyncMock(side_effect=[old_draft, None])
    original = AsyncMock(side_effect=RuntimeError("regeneration failed"))

    with pytest.raises(RuntimeError, match="regeneration failed"):
        await reject_draft_with_request_handoff(
            strategy,
            old_draft.draft_id,
            original,
        )

    assert strategy.register_request_draft.await_args_list[1].args[:2] == (
        "request-1",
        REQUEST_RECORD_FAILED,
    )
