from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from aigateway_core.pipelines.generation._common.models import (
    DRAFT_STATUS_COMPLETED,
    DRAFT_STATUS_RUNNING,
)
from aigateway_core.pipelines.generation.draft.terminal_task_visibility import (
    _join_terminal_confirmation_task,
)


@pytest.mark.asyncio
async def test_terminal_reader_waits_for_confirmation_task_cleanup() -> None:
    release = asyncio.Event()

    async def confirmation() -> None:
        await release.wait()

    task = asyncio.create_task(confirmation(), name="draft-confirm-draft-1")
    strategy = SimpleNamespace(
        _confirmation_tasks={"draft-1": task},
        _bg_tasks={task},
    )
    draft = SimpleNamespace(status=DRAFT_STATUS_COMPLETED)

    read = asyncio.create_task(
        _join_terminal_confirmation_task(strategy, "draft-1", draft)
    )
    await asyncio.sleep(0)
    assert not read.done()

    release.set()
    assert await read is draft
    assert task not in strategy._bg_tasks
    assert "draft-1" not in strategy._confirmation_tasks


@pytest.mark.asyncio
async def test_non_terminal_reader_does_not_wait_for_confirmation_task() -> None:
    never = asyncio.Event()

    async def confirmation() -> None:
        await never.wait()

    task = asyncio.create_task(confirmation(), name="draft-confirm-draft-1")
    strategy = SimpleNamespace(
        _confirmation_tasks={"draft-1": task},
        _bg_tasks={task},
    )
    draft = SimpleNamespace(status=DRAFT_STATUS_RUNNING)

    assert await _join_terminal_confirmation_task(
        strategy,
        "draft-1",
        draft,
    ) is draft
    assert not task.done()

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
