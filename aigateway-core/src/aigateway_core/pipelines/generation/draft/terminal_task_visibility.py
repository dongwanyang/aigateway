"""Synchronize persisted confirmation terminal states with task ownership."""
from __future__ import annotations

import asyncio
import functools
from typing import Any

from aigateway_core.pipelines.generation._common.models import (
    DRAFT_STATUS_COMPLETED,
    DRAFT_STATUS_CONFIRMED,
)

from .draft_generator import DraftGeneratorStrategy

_ORIGINAL_ATTR = "_aigateway_original_get_draft_terminal_visibility"
_WRAPPER_ATTR = "_aigateway_terminal_task_visibility"
_TERMINAL_CONFIRMATION_STATUSES = {
    DRAFT_STATUS_COMPLETED,
    DRAFT_STATUS_CONFIRMED,
}


async def _join_terminal_confirmation_task(
    strategy: Any,
    draft_id: str,
    draft: Any,
) -> Any:
    """Hide a terminal state until its owning confirmation Task has exited.

    Result bytes and ``completed`` metadata are persisted near the end of the
    confirmation coroutine. Without this join, a status reader can observe the
    terminal record during the single event-loop turn before the Task returns
    and its done callback removes it from the ownership registries.
    """
    if getattr(draft, "status", None) not in _TERMINAL_CONFIRMATION_STATUSES:
        return draft
    tasks = getattr(strategy, "_confirmation_tasks", None)
    task = tasks.get(draft_id) if isinstance(tasks, dict) else None
    if task is None or task is asyncio.current_task():
        return draft

    if not task.done():
        try:
            await asyncio.shield(task)
        except (Exception, asyncio.CancelledError):
            # The persisted terminal record remains the source of truth. A
            # late callback exception must not turn a successful status read
            # into a transport error, but ownership cleanup still runs below.
            pass

    background = getattr(strategy, "_bg_tasks", None)
    if isinstance(background, set):
        background.discard(task)
    if isinstance(tasks, dict) and tasks.get(draft_id) is task:
        tasks.pop(draft_id, None)
    return draft


def install_terminal_task_visibility() -> None:
    """Install one idempotent terminal-state visibility wrapper."""
    current = DraftGeneratorStrategy.get_draft
    if getattr(current, _WRAPPER_ATTR, False):
        return
    if not hasattr(DraftGeneratorStrategy, _ORIGINAL_ATTR):
        setattr(DraftGeneratorStrategy, _ORIGINAL_ATTR, current)
    original = getattr(DraftGeneratorStrategy, _ORIGINAL_ATTR)

    @functools.wraps(original)
    async def get_draft_after_task_cleanup(
        self: Any,
        draft_id: str,
    ) -> Any:
        draft = await original(self, draft_id)
        if draft is None:
            return None
        return await _join_terminal_confirmation_task(
            self,
            draft_id,
            draft,
        )

    setattr(get_draft_after_task_cleanup, _WRAPPER_ATTR, True)
    DraftGeneratorStrategy.get_draft = get_draft_after_task_cleanup


__all__ = [
    "install_terminal_task_visibility",
]
