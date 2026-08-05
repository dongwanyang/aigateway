from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation._common.models import DraftResult
from aigateway_core.pipelines.generation.draft import _draft_generator_impl as _impl
from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)


def _video_draft(
    *,
    draft_id: str = "video-draft",
    preview: bytes = b"approved-keyframe",
    generation_params: dict | None = None,
    status: str = "pending",
) -> DraftResult:
    return DraftResult(
        draft_id=draft_id,
        previews=[preview],
        generation_params=generation_params or {},
        created_at=0,
        expires_at=100,
        status=status,
        media_type="video",
    )


def test_confirmation_rollback_cannot_rebaseline_modified_keyframe():
    draft = _video_draft()
    DraftGeneratorStrategy._freeze_video_keyframe(draft)
    frozen_hash = draft.generation_params["source_image_sha256"]

    draft.previews = [b"tampered-keyframe"]
    DraftGeneratorStrategy._freeze_video_keyframe(draft)

    assert draft.generation_params["source_image_sha256"] == frozen_hash
    assert draft.generation_params["source_image_frozen_draft_id"] == draft.draft_id
    with pytest.raises(
        DraftWorkflowError,
        match="video_keyframe_integrity_mismatch",
    ):
        DraftGeneratorStrategy._validate_frozen_video_keyframe(draft)


def test_regenerated_draft_receives_new_frozen_identity():
    original = _video_draft(draft_id="original", preview=b"original")
    DraftGeneratorStrategy._freeze_video_keyframe(original)

    regenerated = _video_draft(
        draft_id="regenerated",
        preview=b"regenerated",
        generation_params=original.generation_params.copy(),
    )
    DraftGeneratorStrategy._freeze_video_keyframe(regenerated)

    assert regenerated.generation_params["source_image_sha256"] == hashlib.sha256(
        b"regenerated"
    ).hexdigest()
    assert (
        regenerated.generation_params["source_image_frozen_draft_id"]
        == "regenerated"
    )


@pytest.mark.asyncio
async def test_legacy_regeneration_rebinds_frozen_identity(monkeypatch):
    old_preview = b"legacy-approved"
    old = _video_draft(
        draft_id="legacy",
        preview=old_preview,
        generation_params={
            "source_image_sha256": hashlib.sha256(old_preview).hexdigest(),
        },
    )
    strategy = object.__new__(DraftGeneratorStrategy)

    async def regenerate(_self, old_draft):
        regenerated = _video_draft(
            draft_id="regenerated",
            preview=b"new-preview",
            generation_params=old_draft.generation_params.copy(),
        )
        DraftGeneratorStrategy._freeze_video_keyframe(regenerated)
        return regenerated

    monkeypatch.setattr(
        _impl.DraftGeneratorStrategy,
        "_regenerate_draft",
        regenerate,
    )

    regenerated = await strategy._regenerate_draft(old)

    assert old.generation_params["source_image_frozen_draft_id"] == "legacy"
    assert regenerated.generation_params["source_image_sha256"] == hashlib.sha256(
        b"new-preview"
    ).hexdigest()
    assert (
        regenerated.generation_params["source_image_frozen_draft_id"]
        == "regenerated"
    )


def test_frozen_identity_rejects_digest_from_another_draft():
    preview = b"same-reference-image"
    draft = _video_draft(
        generation_params={
            "source_image_sha256": hashlib.sha256(preview).hexdigest(),
            "source_image_frozen_draft_id": "different-draft",
        },
        preview=preview,
    )

    with pytest.raises(
        DraftWorkflowError,
        match="video_keyframe_integrity_mismatch",
    ):
        DraftGeneratorStrategy._validate_frozen_video_keyframe(draft)


@pytest.mark.asyncio
async def test_claim_validates_keyframe_before_worker_scheduling(monkeypatch):
    draft = _video_draft(
        preview=b"tampered",
        generation_params={
            "source_image_sha256": hashlib.sha256(b"approved").hexdigest(),
            "source_image_frozen_draft_id": "video-draft",
        },
        status="refining",
    )
    strategy = object.__new__(DraftGeneratorStrategy)
    strategy._mark_draft_confirmation_failed = AsyncMock(return_value=None)

    async def claim(_self, draft_id):
        assert draft_id == draft.draft_id
        return draft, True

    monkeypatch.setattr(
        _impl.DraftGeneratorStrategy,
        "_claim_draft_confirmation",
        claim,
    )

    with pytest.raises(
        DraftWorkflowError,
        match="video_keyframe_integrity_mismatch",
    ):
        await strategy._claim_draft_confirmation(draft.draft_id)

    strategy._mark_draft_confirmation_failed.assert_awaited_once_with(
        draft,
        "video_keyframe_integrity_mismatch",
    )


@pytest.mark.asyncio
async def test_concurrent_confirmations_share_one_inflight_task():
    strategy = object.__new__(DraftGeneratorStrategy)
    strategy._confirmation_task_lock = asyncio.Lock()
    strategy._confirmation_tasks = {}
    strategy._bg_tasks = set()

    started = asyncio.Event()
    release = asyncio.Event()
    result = SimpleNamespace(output_data=b"video")
    call_count = 0

    async def confirm_impl(draft_id):
        nonlocal call_count
        assert draft_id == "video-draft"
        call_count += 1
        started.set()
        await release.wait()
        return result

    strategy._confirm_draft_impl = confirm_impl

    first = asyncio.create_task(strategy.confirm_draft("video-draft"))
    await started.wait()
    second = asyncio.create_task(strategy.confirm_draft("video-draft"))
    await asyncio.sleep(0)

    assert call_count == 1
    assert len(strategy._confirmation_tasks) == 1

    release.set()
    first_result, second_result = await asyncio.gather(first, second)
    await asyncio.sleep(0)

    assert first_result is result
    assert second_result is result
    assert call_count == 1
    assert strategy._confirmation_tasks == {}
