from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation._common.models import (
    DRAFT_STATUS_COMPLETED,
    DRAFT_STATUS_PENDING,
    DraftResult,
)
from aigateway_core.pipelines.generation.draft.source_draft_video import (
    create_video_draft_from_source,
)


class FakeStrategy:
    def __init__(self, root: Path, source: DraftResult, result: bytes) -> None:
        self._root = root
        self._source = source
        self._result = result
        self._stored: DraftResult | None = None
        self.dependency_request = None
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
        if draft_id != self._source.draft_id:
            raise DraftWorkflowError("Draft not found or expired")
        return self._result

    async def check_local_dependencies(self, request) -> None:
        self.dependency_request = request

    def _ensure_draft_dir(self, session_id: str, draft_id: str) -> Path:
        path = self._root / session_id / draft_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_reference_bytes(self, draft_dir: Path, data: bytes) -> None:
        (draft_dir / "reference.png").write_bytes(data)

    async def _store_draft(self, draft: DraftResult, _ttl_seconds: float) -> None:
        self._stored = draft


def source_image_draft(**overrides) -> DraftResult:
    values = {
        "draft_id": "source-image",
        "previews": [b"preview"],
        "generation_params": {"prompt": "一只柯基站在草地上"},
        "created_at": 1.0,
        "expires_at": 9999999999.0,
        "status": DRAFT_STATUS_COMPLETED,
        "media_type": "image",
        "session_id": "session-1",
        "user_id": "user-1",
        "group_id": "group-1",
    }
    values.update(overrides)
    return DraftResult(**values)


@pytest.mark.asyncio
async def test_source_result_is_copied_and_frozen_byte_for_byte(tmp_path):
    source_bytes = b"exact-source-result-bytes"
    strategy = FakeStrategy(tmp_path, source_image_draft(), source_bytes)

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
    assert draft.status == DRAFT_STATUS_PENDING
    assert draft.media_type == "video"
    assert draft.previews == [source_bytes]
    assert draft.generation_params["source_draft_id"] == "source-image"
    assert draft.generation_params["source_kind"] == "draft_result"
    assert draft.generation_params["source_image_sha256"] == expected_hash
    assert draft.generation_params["source_result_sha256"] == expected_hash
    assert draft.generation_params["frame_count"] == 41
    assert draft.generation_params["motion_prompt"] == "柯基摇动尾巴并向镜头跑来"
    assert strategy._stored is draft
    assert strategy.dependency_request.reference_images[0].raw_data == source_bytes
    copied = tmp_path / "session-1" / draft.draft_id / "reference.png"
    assert copied.read_bytes() == source_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "user_id", "group_id", "session_id", "expected"),
    [
        (source_image_draft(), "other-user", "group-1", "session-1", "source_draft_forbidden"),
        (source_image_draft(), "user-1", "other-group", "session-1", "source_draft_forbidden"),
        (source_image_draft(), "user-1", "group-1", "other-session", "source_draft_forbidden"),
        (source_image_draft(status="pending"), "user-1", "group-1", "session-1", "source_draft_invalid_type"),
        (source_image_draft(media_type="video"), "user-1", "group-1", "session-1", "source_draft_invalid_type"),
    ],
)
async def test_source_draft_authorization_and_type_are_enforced(
    tmp_path,
    source,
    user_id,
    group_id,
    session_id,
    expected,
):
    strategy = FakeStrategy(tmp_path, source, b"source")

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
async def test_motion_prompt_and_duration_are_validated_before_creation(tmp_path):
    strategy = FakeStrategy(tmp_path, source_image_draft(), b"source")

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
