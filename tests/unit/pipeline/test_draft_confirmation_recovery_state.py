"""Regression coverage for confirmation rollback state cleanup."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)


@pytest.mark.asyncio
async def test_confirmation_failure_clears_stale_prompt_and_comfy_progress() -> None:
    """A retryable confirmation rollback must not retain the old ComfyUI job."""
    strategy = object.__new__(DraftGeneratorStrategy)
    strategy._store_draft = AsyncMock()
    strategy._draft_cancel_requested = AsyncMock(return_value=False)
    draft = SimpleNamespace(
        draft_id="draft-1",
        status="refining",
        stage="sampling 6/12",
        progress=0.5,
        comfy_prompt_id="stale-prompt",
        generation_params={"progress_source": "comfyui"},
        expires_at=time.time() + 3600,
    )

    await strategy._mark_draft_confirmation_failed(
        draft,
        "ComfyUI refinement job disappeared",
    )

    assert draft.status == "pending"
    assert draft.stage == "pending"
    assert draft.progress == 1.0
    assert draft.comfy_prompt_id is None
    assert draft.generation_params["progress_source"] == "stage"
    assert draft.generation_params["last_confirm_error"] == (
        "ComfyUI refinement job disappeared"
    )
    strategy._draft_cancel_requested.assert_awaited_once_with("draft-1")
    strategy._store_draft.assert_awaited_once()
