from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aigateway_core.pipelines.generation._common.exceptions import (
    DraftWorkflowError,
)
from aigateway_core.pipelines.generation._common.models import (
    DRAFT_STATUS_COMPLETED,
    DRAFT_STATUS_PENDING,
    DraftResult,
)
from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)
from aigateway_core.pipelines.generation.draft.source_draft_video import (
    create_video_draft_from_source,
)


class FakeStrategy:
    def __init__(self, source: DraftResult, result: bytes) -> None:
        self._source = source
        self._result = result
        self._stored: DraftResult | None = None
        self.dependency_request = None
        self.dependency_error: Exception | None = None
        self.result_error: Exception | None = None
        self._config = SimpleNamespace(
            video_supported_durations_seconds=(3, 5, 8),
            video_max_fps=60,
            video_min_frames=1,
            video_max_frames=481,
            default_target_resolution=(1920, 1080),
            retention_period_hours=24,
            max_regeneration_attempts=5,
        )
        self._comfyui_config = SimpleNamespace(
            workflow_version="image-v1",
            video_workflow_version="wan-v1",
            video_required_vram_gb=12.0,
        )

    async def get_draft(self, draft_id: str):
        return self._source if draft_id == self._source.draft_id else None

    async def get_result_bytes(self, draft_id: str) -> bytes:
        if self.result_error is not None:
            raise self.result_error
        if draft_id != self._source.draft_id:
            raise DraftWorkflowError("Draft not found or expired")
        return self._result

    async def check_local_dependencies(self, request) -> None:
        self.dependency_request = request
        if self.dependency_error is not None:
            raise self.dependency_error

    async def _store_draft(self, draft: DraftResult, _ttl_seconds: float) -> None:
        self._stored = draft


def source_image_draft(**overrides) -> DraftResult:
    values = {
        "draft_id": "source-image",
        "previews": [b"preview"],
        "generation_params": {"prompt": "一只柯基站在草地上"},
        "created_at": 1.0,
        "expires_at": 9_999_999_999.0,
        "status": DRAFT_STATUS_COMPLETED,
        "media_type": "image",
        "session_id": "session-1",
        "user_id": "user-1",
        "group_id": "group-1",
    }
    values.update(overrides)
    return DraftResult(**values)


@pytest.mark.asyncio
async def test_source_result_is_copied_and_frozen_byte_for_byte():
    source_bytes = b"exact-source-result-bytes"
    strategy = FakeStrategy(source_image_draft(), source_bytes)

    draft = await create_video_draft_from_source(
        strategy,
        source_draft_id="source-image",
        motion_prompt="柯基摇动尾巴并向镜头跑来",
        duration_seconds=5,
        fps=8,
        chat_session_id="session-1",
        user_id="user-1",
        group_id="group-1",
        trace_id="trace-1",
    )

    expected_hash = hashlib.sha256(source_bytes).hexdigest()
    motion_prompt = draft.generation_params["motion_prompt"]
    assert draft.status == DRAFT_STATUS_PENDING
    assert draft.media_type == "video"
    assert draft.previews == [source_bytes]
    assert draft.generation_params["source_draft_id"] == "source-image"
    assert draft.generation_params["source_kind"] == "draft_result"
    assert draft.generation_params["source_image_sha256"] == expected_hash
    assert draft.generation_params["source_result_sha256"] == expected_hash
    assert draft.generation_params["source_image_frozen_draft_id"] == draft.draft_id
    assert draft.generation_params["frame_count"] == 41
    assert draft.generation_params["source_prompt"] == "柯基摇动尾巴并向镜头跑来"
    assert motion_prompt.startswith("柯基摇动尾巴并向镜头跑来")
    assert "保持主体身份" in motion_prompt
    assert "不切换场景" in motion_prompt
    assert strategy._stored is draft
    assert strategy.dependency_request.reference_images[0].raw_data == source_bytes
    assert strategy.dependency_request.motion_prompt == motion_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "user_id", "group_id", "session_id", "expected"),
    [
        (
            source_image_draft(),
            "other-user",
            "group-1",
            "session-1",
            "source_draft_forbidden",
        ),
        (
            source_image_draft(),
            "user-1",
            "other-group",
            "session-1",
            "source_draft_forbidden",
        ),
        (
            source_image_draft(),
            "user-1",
            "group-1",
            "other-session",
            "source_draft_forbidden",
        ),
        (
            source_image_draft(user_id=None, group_id=None),
            None,
            None,
            "session-1",
            "source_draft_forbidden",
        ),
        (
            source_image_draft(status="pending"),
            "user-1",
            "group-1",
            "session-1",
            "source_draft_invalid_type",
        ),
        (
            source_image_draft(media_type="video"),
            "user-1",
            "group-1",
            "session-1",
            "source_draft_invalid_type",
        ),
    ],
)
async def test_source_draft_authorization_and_type_are_enforced(
    source,
    user_id,
    group_id,
    session_id,
    expected,
):
    strategy = FakeStrategy(source, b"source")

    with pytest.raises(DraftWorkflowError, match=expected):
        await create_video_draft_from_source(
            strategy,
            source_draft_id=source.draft_id,
            motion_prompt="move",
            duration_seconds=5,
            fps=8,
            chat_session_id=session_id,
            user_id=user_id,
            group_id=group_id,
        )


@pytest.mark.asyncio
async def test_motion_prompt_and_duration_are_validated_before_creation():
    strategy = FakeStrategy(source_image_draft(), b"source")

    with pytest.raises(DraftWorkflowError, match="video_motion_prompt_missing"):
        await create_video_draft_from_source(
            strategy,
            source_draft_id="source-image",
            motion_prompt="  ",
            duration_seconds=5,
            fps=8,
            chat_session_id="session-1",
            user_id="user-1",
            group_id="group-1",
        )

    with pytest.raises(DraftWorkflowError, match="video_duration_unsupported"):
        await create_video_draft_from_source(
            strategy,
            source_draft_id="source-image",
            motion_prompt="move",
            duration_seconds=4,
            fps=8,
            chat_session_id="session-1",
            user_id="user-1",
            group_id="group-1",
        )


@pytest.mark.asyncio
async def test_dependency_failure_does_not_persist_a_video_draft():
    strategy = FakeStrategy(source_image_draft(), b"source")
    strategy.dependency_error = DraftWorkflowError(
        "comfyui_missing_dependencies: diffusion_models/wan.safetensors"
    )

    with pytest.raises(DraftWorkflowError, match="comfyui_missing_dependencies"):
        await create_video_draft_from_source(
            strategy,
            source_draft_id="source-image",
            motion_prompt="move",
            duration_seconds=5,
            fps=8,
            chat_session_id="session-1",
            user_id="user-1",
            group_id="group-1",
        )

    assert strategy._stored is None


@pytest.mark.asyncio
async def test_storage_failure_is_not_mapped_to_source_not_found():
    strategy = FakeStrategy(source_image_draft(), b"source")
    strategy.result_error = OSError("permission denied")

    with pytest.raises(OSError, match="permission denied"):
        await create_video_draft_from_source(
            strategy,
            source_draft_id="source-image",
            motion_prompt="move",
            duration_seconds=5,
            fps=8,
            chat_session_id="session-1",
            user_id="user-1",
            group_id="group-1",
        )

    assert strategy._stored is None


@pytest.mark.asyncio
async def test_source_result_video_draft_cannot_regenerate_keyframe():
    strategy = object.__new__(DraftGeneratorStrategy)
    draft = DraftResult(
        draft_id="source-video",
        previews=[b"source"],
        generation_params={"source_kind": "draft_result"},
        created_at=1.0,
        expires_at=9_999_999_999.0,
        status=DRAFT_STATUS_PENDING,
        media_type="video",
    )
    strategy.get_draft = AsyncMock(return_value=draft)

    with pytest.raises(DraftWorkflowError, match="source_draft_immutable"):
        await strategy.reject_draft(draft.draft_id)
