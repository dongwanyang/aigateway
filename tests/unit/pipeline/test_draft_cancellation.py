"""Draft cancellation must stop the work a departed client left behind.

Regression: aborting the chat request only tore down the browser's fetch. The
draft ran in a detached ``asyncio`` task that kept its ComfyUI job queued, so on
a single-GPU host abandoned drafts starved the draft the UI was still polling
until it hit ``generation_wait_timeout``.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from aigateway_core.pipelines.generation._common.models import (
    DRAFT_STATUS_CANCELLED,
    DRAFT_STATUS_COMPLETED,
    DRAFT_STATUS_RUNNING,
    DraftResult,
)
from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)


class _Strategy(DraftGeneratorStrategy):
    """Exercise cancel_draft against in-memory draft state."""

    def __init__(self) -> None:
        self._drafts: dict[str, DraftResult] = {}
        self._bg_tasks: set[asyncio.Task] = set()
        self._task_tracker = None
        self._gpu_coordinator = None
        self.cancelled_prompts: list[str] = []
        self.traces: list[str] = []

    async def _load_draft(self, draft_id):
        return self._drafts.get(draft_id)

    async def _store_draft(self, draft, ttl_seconds):
        self._drafts[draft.draft_id] = draft

    async def _cancel_comfyui_workflow(self, prompt_id, *, server_url=None):
        self.cancelled_prompts.append(prompt_id)
        return True

    async def _emit_draft_trace(self, trace_id, name, **kwargs):
        self.traces.append(name)


def _draft(draft_id: str, *, status: str, prompt_id: str | None = None):
    return DraftResult(
        draft_id=draft_id,
        previews=[],
        generation_params={"trace_id": "trace-cancel"},
        created_at=time.time(),
        expires_at=time.time() + 3600,
        attempt_number=1,
        max_attempts=5,
        status=status,
        media_type="image",
        comfy_prompt_id=prompt_id,
    )


@pytest.mark.asyncio
async def test_cancel_stops_background_task_and_comfyui_job():
    strategy = _Strategy()
    draft_id = "draft-running"
    strategy._drafts[draft_id] = _draft(
        draft_id, status=DRAFT_STATUS_RUNNING, prompt_id="prompt-1"
    )

    started = asyncio.Event()

    async def never_finishes() -> None:
        started.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(
        never_finishes(), name=f"draft-generate-{draft_id}"
    )
    strategy._bg_tasks.add(task)
    await started.wait()

    assert await strategy.cancel_draft(draft_id) is True

    # The orphaned generation must actually stop, not just be relabelled.
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert strategy.cancelled_prompts == ["prompt-1"]

    stored = strategy._drafts[draft_id]
    assert stored.status == DRAFT_STATUS_CANCELLED
    assert stored.error == "draft_cancelled"
    assert "draft.cancelled" in strategy.traces


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_leaves_terminal_drafts_alone():
    strategy = _Strategy()
    strategy._drafts["done"] = _draft("done", status=DRAFT_STATUS_COMPLETED)

    assert await strategy.cancel_draft("done") is False
    assert strategy._drafts["done"].status == DRAFT_STATUS_COMPLETED
    assert strategy.cancelled_prompts == []

    strategy._drafts["live"] = _draft("live", status=DRAFT_STATUS_RUNNING)
    assert await strategy.cancel_draft("live") is True
    # A second cancel must not re-run ComfyUI teardown or flip state again.
    assert await strategy.cancel_draft("live") is False


@pytest.mark.asyncio
async def test_cancel_unknown_draft_reports_false():
    strategy = _Strategy()
    assert await strategy.cancel_draft("missing") is False


@pytest.mark.asyncio
async def test_cancel_only_touches_its_own_background_task():
    strategy = _Strategy()
    strategy._drafts["mine"] = _draft("mine", status=DRAFT_STATUS_RUNNING)

    async def other() -> None:
        await asyncio.sleep(3600)

    unrelated = asyncio.create_task(other(), name="draft-generate-other")
    strategy._bg_tasks.add(unrelated)
    await asyncio.sleep(0)

    assert await strategy.cancel_draft("mine") is True
    assert not unrelated.cancelled()

    unrelated.cancel()
    with pytest.raises(asyncio.CancelledError):
        await unrelated
