from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aigateway_api import verified_draft_cancellation as cancellation
from aigateway_core.pipelines.generation._common.config import DraftWorkflowConfig
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation._common.models import (
    DRAFT_STATUS_CANCELLED,
    DRAFT_STATUS_RUNNING,
    DraftResult,
)
from aigateway_core.pipelines.generation.draft import (
    _draft_generator_impl as base_impl,
)
from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)
from aigateway_core.shared.integration_configs import ComfyUIConfig


@pytest.fixture
def strategy(tmp_path) -> DraftGeneratorStrategy:
    return DraftGeneratorStrategy(
        DraftWorkflowConfig(
            enabled=True,
            store_dir=str(tmp_path / "drafts"),
            retention_period_hours=24,
        ),
        redis_client=None,
        comfyui_config=ComfyUIConfig(
            workflow_version="image-v1",
            checkpoint_name="test-model.safetensors",
            allowed_checkpoints=["test-model.safetensors"],
            checkpoint_vram_gb={"test-model.safetensors": 8.0},
            sdxl_required_vram_gb=8.0,
        ),
    )


def _running_draft() -> DraftResult:
    now = time.time()
    return DraftResult(
        draft_id="draft-1",
        previews=[],
        generation_params={"request_id": "request-1", "trace_id": "trace-1"},
        created_at=now,
        expires_at=now + 3600,
        status=DRAFT_STATUS_RUNNING,
        media_type="video",
        session_id="session-1",
        user_id="user-1",
        group_id=None,
        progress=0.4,
        stage="waiting_for_comfyui",
        workflow_version="wan22-v1",
        comfy_prompt_id="prompt-1",
        worker_id="worker-1",
        device_uuid="gpu-1",
    )


async def _fake_original_cancel(
    strategy: DraftGeneratorStrategy,
    draft_id: str,
) -> DraftResult:
    draft = await strategy.get_draft(draft_id)
    assert draft is not None
    draft.status = DRAFT_STATUS_CANCELLED
    draft.stage = "cancelled"
    draft.progress = 0.0
    draft.error = "cancelled"
    draft.comfy_prompt_id = None
    await base_impl.DraftGeneratorStrategy._store_draft(strategy, draft, 3600)
    return draft


@pytest.mark.asyncio
async def test_local_task_is_cancelled_only_after_prompt_release(
    strategy: DraftGeneratorStrategy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = _running_draft()
    await base_impl.DraftGeneratorStrategy._store_draft(strategy, draft, 3600)
    cancel_upstream = AsyncMock()
    monkeypatch.setattr(strategy, "_cancel_comfy_prompt", cancel_upstream)

    async def released(*_args, **_kwargs) -> bool:
        return True

    monkeypatch.setattr(cancellation, "_prompt_released", released)
    original = AsyncMock(side_effect=_fake_original_cancel)

    result = await cancellation.cancel_draft_verified(
        strategy,
        draft.draft_id,
        original,
    )

    cancel_upstream.assert_awaited_once_with(
        "prompt-1",
        server_url=None,
    )
    original.assert_awaited_once_with(strategy, draft.draft_id)
    assert result.status == DRAFT_STATUS_CANCELLED
    stored = await strategy.get_draft(draft.draft_id)
    assert stored is not None
    assert stored.status == DRAFT_STATUS_CANCELLED
    assert stored.comfy_prompt_id is None


@pytest.mark.asyncio
async def test_unconfirmed_release_keeps_local_task_and_fences_worker(
    strategy: DraftGeneratorStrategy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = _running_draft()
    await base_impl.DraftGeneratorStrategy._store_draft(strategy, draft, 3600)
    await strategy._set_cancel_record(
        "request-1",
        user_id="user-1",
        group_id=None,
        session_id="session-1",
        ttl_seconds=3600,
    )
    worker = SimpleNamespace(
        worker_id="worker-1",
        device_uuid="gpu-1",
        server_url="http://comfyui:8188",
        queue_running=0,
        queue_pending=0,
    )
    events: list[tuple[str, str, str]] = []
    strategy._gpu_coordinator = SimpleNamespace(
        get_worker=lambda worker_id: worker if worker_id == "worker-1" else None,
        _condition=asyncio.Condition(),
        record_event=lambda event, *, worker_id="", device_uuid="": events.append(
            (event, worker_id, device_uuid)
        ),
    )
    cancel_upstream = AsyncMock()
    monkeypatch.setattr(strategy, "_cancel_comfy_prompt", cancel_upstream)

    async def not_released(*_args, **_kwargs) -> bool:
        return False

    monkeypatch.setattr(cancellation, "_prompt_released", not_released)
    original = AsyncMock(side_effect=_fake_original_cancel)

    with pytest.raises(
        DraftWorkflowError,
        match="comfyui_cancellation_unconfirmed",
    ):
        await cancellation.cancel_draft_verified(
            strategy,
            draft.draft_id,
            original,
        )

    cancel_upstream.assert_awaited_once_with(
        "prompt-1",
        server_url="http://comfyui:8188",
    )
    original.assert_not_awaited()
    stored = await strategy.get_draft(draft.draft_id)
    assert stored is not None
    assert stored.status == DRAFT_STATUS_RUNNING
    assert stored.stage == "cancellation_unconfirmed"
    assert stored.error == "comfyui_cancellation_unconfirmed"
    assert stored.comfy_prompt_id == "prompt-1"
    assert stored.generation_params["last_cancel_error"] == (
        "comfyui_cancellation_unconfirmed"
    )
    assert await strategy._cancel_record("request-1") is None
    assert worker.queue_running == 1
    assert events == [("cancellation_unconfirmed", "worker-1", "gpu-1")]


@pytest.mark.asyncio
async def test_registered_request_skips_early_tombstone(
    strategy: DraftGeneratorStrategy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = _running_draft()
    record = {
        "draft_id": draft.draft_id,
        "user_id": "user-1",
        "group_id": None,
        "session_id": "session-1",
    }
    resolve = AsyncMock(return_value=(draft, record))
    monkeypatch.setattr(strategy, "resolve_request", resolve)
    cancel_draft = AsyncMock(return_value=draft)
    monkeypatch.setattr(strategy, "cancel_draft", cancel_draft)
    original_request = AsyncMock()

    result = await cancellation.cancel_request_verified(
        strategy,
        "request-1",
        user_id="user-1",
        group_id=None,
        session_id="session-1",
        original=original_request,
    )

    assert result is draft
    cancel_draft.assert_awaited_once_with(draft.draft_id)
    original_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_pre_registration_request_keeps_tombstone_path(
    strategy: DraftGeneratorStrategy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve = AsyncMock(return_value=(None, None))
    monkeypatch.setattr(strategy, "resolve_request", resolve)
    original_request = AsyncMock(return_value=None)

    result = await cancellation.cancel_request_verified(
        strategy,
        "request-before-submit",
        user_id="user-1",
        group_id=None,
        session_id="session-1",
        original=original_request,
    )

    assert result is None
    original_request.assert_awaited_once_with(
        strategy,
        "request-before-submit",
        user_id="user-1",
        group_id=None,
        session_id="session-1",
    )


@pytest.mark.asyncio
async def test_registered_request_rejects_wrong_owner_without_tombstone(
    strategy: DraftGeneratorStrategy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = _running_draft()
    record = {
        "draft_id": draft.draft_id,
        "user_id": "user-1",
        "group_id": None,
        "session_id": "session-1",
    }
    monkeypatch.setattr(
        strategy,
        "resolve_request",
        AsyncMock(return_value=(draft, record)),
    )
    original_request = AsyncMock()

    with pytest.raises(
        DraftWorkflowError,
        match="generation_request_forbidden",
    ):
        await cancellation.cancel_request_verified(
            strategy,
            "request-1",
            user_id="user-2",
            group_id=None,
            session_id="session-1",
            original=original_request,
        )

    original_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_prompt_requires_no_external_verification(
    strategy: DraftGeneratorStrategy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = _running_draft()
    draft.comfy_prompt_id = None
    await base_impl.DraftGeneratorStrategy._store_draft(strategy, draft, 3600)

    async def unexpected(*_args, **_kwargs) -> bool:
        raise AssertionError("prompt verification must not run without a prompt")

    monkeypatch.setattr(cancellation, "_prompt_released", unexpected)
    cancel_upstream = AsyncMock()
    monkeypatch.setattr(strategy, "_cancel_comfy_prompt", cancel_upstream)
    original = AsyncMock(side_effect=_fake_original_cancel)

    result = await cancellation.cancel_draft_verified(
        strategy,
        draft.draft_id,
        original,
    )

    cancel_upstream.assert_not_awaited()
    original.assert_awaited_once_with(strategy, draft.draft_id)
    assert result.status == DRAFT_STATUS_CANCELLED
