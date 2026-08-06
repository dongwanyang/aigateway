"""Keep request-id recovery and cancellation atomic across draft rejection."""
from __future__ import annotations

import functools
import time
from typing import Any, Awaitable, Callable

from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)

from .generation_request_state import REQUEST_RECORD_FAILED

_ORIGINAL_ATTR = "_aigateway_original_reject_draft"
_WRAPPER_ATTR = "_aigateway_request_handoff_reject_draft"


def _request_id(draft: Any) -> str:
    params = getattr(draft, "generation_params", {})
    return str(params.get("request_id") or "").strip() if isinstance(params, dict) else ""


def _ttl_seconds(draft: Any) -> int:
    return max(1, int(float(getattr(draft, "expires_at", 0.0) or 0.0) - time.time()))


async def _register(
    strategy: Any,
    request_id: str,
    draft_id: str,
    owner: Any,
) -> None:
    await strategy.register_request_draft(
        request_id,
        draft_id,
        user_id=getattr(owner, "user_id", None),
        group_id=getattr(owner, "group_id", None),
        session_id=getattr(owner, "session_id", None),
        ttl_seconds=_ttl_seconds(owner),
    )


async def reject_draft_with_request_handoff(
    strategy: Any,
    draft_id: str,
    original: Callable[[Any, str], Awaitable[Any]],
) -> Any:
    """Move request recovery from the old draft to its regenerated successor.

    The pending record is written before the old draft is deleted. A concurrent
    Stop then creates an owner-scoped cancellation tombstone; once the new draft
    is registered, that tombstone is applied and its just-created background
    task is cancelled before the operation returns.
    """
    old_draft = await strategy.get_draft(draft_id)
    if old_draft is None:
        return await original(strategy, draft_id)
    request_id = _request_id(old_draft)
    if not request_id:
        return await original(strategy, draft_id)

    # Do not leave the request mapping pointing at a draft that the underlying
    # rejection flow is about to delete.
    await _register(strategy, request_id, "", old_draft)
    try:
        new_draft = await original(strategy, draft_id)
    except Exception:
        surviving_old = await strategy.get_draft(draft_id)
        if surviving_old is not None:
            await _register(strategy, request_id, draft_id, surviving_old)
        else:
            await _register(
                strategy,
                request_id,
                REQUEST_RECORD_FAILED,
                old_draft,
            )
        raise

    await _register(strategy, request_id, new_draft.draft_id, new_draft)
    cancel_record = await strategy._cancel_record(request_id)
    if cancel_record and strategy._record_matches_owner(
        cancel_record,
        user_id=getattr(new_draft, "user_id", None),
        group_id=getattr(new_draft, "group_id", None),
        session_id=getattr(new_draft, "session_id", None),
    ):
        # _regenerate_draft may already have persisted cancelled through the
        # tombstone guard, but cancel_draft is intentionally idempotent and also
        # terminates the newly-created local background Task.
        new_draft = await strategy.cancel_draft(new_draft.draft_id)
    return new_draft


def install_draft_rejection_lifecycle() -> None:
    """Install one idempotent request-handoff wrapper around rejection."""
    current = DraftGeneratorStrategy.reject_draft
    if getattr(current, _WRAPPER_ATTR, False):
        return
    if not hasattr(DraftGeneratorStrategy, _ORIGINAL_ATTR):
        setattr(DraftGeneratorStrategy, _ORIGINAL_ATTR, current)
    original = getattr(DraftGeneratorStrategy, _ORIGINAL_ATTR)

    @functools.wraps(original)
    async def reject_with_handoff(self: Any, draft_id: str) -> Any:
        return await reject_draft_with_request_handoff(
            self,
            draft_id,
            original,
        )

    setattr(reject_with_handoff, _WRAPPER_ATTR, True)
    DraftGeneratorStrategy.reject_draft = reject_with_handoff


__all__ = [
    "install_draft_rejection_lifecycle",
    "reject_draft_with_request_handoff",
]
