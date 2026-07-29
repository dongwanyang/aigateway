"""
Tests for DraftGeneratorStrategy — 渐进式生成工作流核心逻辑
==========================================================

验证:
- 图片请求生成 512x512 预览
- 视频请求先生成一张低成本关键帧，确认后再运行图生视频工作流
- confirm_draft: 触发 Upscaler 放大到目标分辨率
- reject_draft: 重新生成草图，不缓存被拒绝的草图
- 重试次数限制，耗尽后返回错误并保留最近草图
- draft_id 唯一标识，24 小时过期自动释放

需求: 3.1, 3.2, 3.3, 3.4, 3.5, 3.7, 3.8, 3.9
"""

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.pipelines.generation._common.config import DraftWorkflowConfig
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation._common.models import (
    DRAFT_STATUS_COMPLETED,
    DRAFT_STATUS_FAILED,
    DRAFT_STATUS_GENERATING,
    DRAFT_STATUS_PENDING,
    DRAFT_STATUS_QUEUED,
    DRAFT_STATUS_REFINING,
    DRAFT_STATUS_RUNNING,
    DraftResult,
    GenerationRequest,
    UpscaleResult,
    VideoSubmitResult,
)
from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)


@pytest.fixture
def default_config(tmp_path):
    """Default Draft workflow config (store_dir 指向 tmp_path 避免写 /app)."""
    return DraftWorkflowConfig(
        enabled=True,
        draft_resolution=(512, 512),
        default_target_resolution=(1920, 1080),
        max_target_resolution=(4096, 4096),
        max_regeneration_attempts=5,
        retention_period_hours=24,
        preview_video_duration_seconds=30,
        preview_keyframe_interval_seconds=5,
        preview_video_fps=8,
        target_fps=60,
        target_fps_range=(24, 120),
        upscale_algorithm="real-esrgan",
        store_dir=str(tmp_path / "drafts"),
    )


@pytest.fixture
def strategy(default_config, monkeypatch):
    """Create a DraftGeneratorStrategy instance with in-memory store + tmp store_dir."""
    instance = DraftGeneratorStrategy(config=default_config, redis_client=None)
    monkeypatch.setattr(instance, "_check_comfyui", AsyncMock(return_value=None))
    monkeypatch.setattr(
        instance,
        "_generate_image_preview_with_comfyui",
        AsyncMock(return_value=b"\x89PNG\r\n\x1a\npreview"),
    )
    monkeypatch.setattr(
        instance,
        "_generate_video_previews_with_comfyui",
        AsyncMock(
            side_effect=lambda _request, _config, **_kwargs: [
                b"\x89PNG\r\n\x1a\nframe"
            ]
        ),
    )
    monkeypatch.setattr(
        instance,
        "_upscale_with_comfyui",
        AsyncMock(return_value=b"\x89PNG\r\n\x1a\nrefined"),
    )
    return instance


@pytest.fixture
def image_request():
    """Create an image generation request."""
    return GenerationRequest(
        prompt="A beautiful sunset over the ocean",
        target_resolution=(1920, 1080),
    )


@pytest.fixture
def video_request():
    """Create a video generation request."""
    return GenerationRequest(
        prompt="A video of a cat playing with a ball",
        target_resolution=(1920, 1080),
        target_fps=60,
        media_type="video",
    )


async def _await_generating(strategy, draft_id, timeout=5.0):
    """轮询 get_draft 直到 generating 后台任务完成（status != generating）.

    异步生成拆分后，generate_draft 立即返回 generating；后台 task 跑完才 pending。
    测试用此 helper 等待终态。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = await strategy.get_draft(draft_id)
        if d is not None and d.status not in {
            DRAFT_STATUS_GENERATING,
            DRAFT_STATUS_QUEUED,
            DRAFT_STATUS_RUNNING,
            DRAFT_STATUS_REFINING,
        }:
            return d
        await asyncio.sleep(0.01)
    pytest.fail(
        f"draft {draft_id} did not leave generating within {timeout}s; "
        f"background_tasks={len(strategy._bg_tasks)}"
    )


# ===================================================================
# Test: generate_draft for image requests
# ===================================================================


class TestGenerateDraftImage:
    """Tests for image draft generation."""

    @pytest.mark.asyncio
    async def test_generates_single_preview(self, strategy, image_request, default_config):
        """Image request should produce exactly one preview (after async generation)."""
        result = await strategy.generate_draft(image_request, default_config)
        # submit_draft 立即返回 generating，previews 空
        assert result.status == DRAFT_STATUS_QUEUED
        assert len(result.previews) == 0

        # 后台 task 完成后 status=pending，previews 落盘
        final = await _await_generating(strategy, result.draft_id)
        assert final is not None
        assert final.status == DRAFT_STATUS_PENDING
        assert len(final.previews) == 1

    @pytest.mark.asyncio
    async def test_draft_id_is_unique(self, strategy, image_request, default_config):
        """Each draft should have a unique ID."""
        result1 = await strategy.generate_draft(image_request, default_config)
        result2 = await strategy.generate_draft(image_request, default_config)

        assert result1.draft_id != result2.draft_id

    @pytest.mark.asyncio
    async def test_status_is_pending(self, strategy, image_request, default_config):
        """After async generation completes, draft status should be 'pending'."""
        result = await strategy.generate_draft(image_request, default_config)
        assert result.status == DRAFT_STATUS_QUEUED

        final = await _await_generating(strategy, result.draft_id)
        assert final.status == DRAFT_STATUS_PENDING

    @pytest.mark.asyncio
    async def test_attempt_number_is_one(self, strategy, image_request, default_config):
        """First draft attempt number should be 1."""
        result = await strategy.generate_draft(image_request, default_config)

        assert result.attempt_number == 1

    @pytest.mark.asyncio
    async def test_expires_at_24h(self, strategy, image_request, default_config):
        """Draft should expire approximately 24 hours from creation."""
        result = await strategy.generate_draft(image_request, default_config)

        expected_ttl = 24 * 3600
        actual_ttl = result.expires_at - result.created_at
        assert abs(actual_ttl - expected_ttl) < 2  # within 2 seconds tolerance

    @pytest.mark.asyncio
    async def test_max_attempts_from_config(self, strategy, image_request, default_config):
        """max_attempts should match config."""
        result = await strategy.generate_draft(image_request, default_config)

        assert result.max_attempts == 5


# ===================================================================
# Test: generate_draft for video requests
# ===================================================================


class TestGenerateDraftVideo:
    """Tests for low-cost video keyframe draft generation."""

    @pytest.mark.asyncio
    async def test_default_keyframe_count(self, strategy, video_request, default_config):
        """Video draft uses one approved keyframe before expensive generation."""
        result = await strategy.generate_draft(video_request, default_config)
        final = await _await_generating(strategy, result.draft_id)

        assert len(final.previews) == 1

    @pytest.mark.asyncio
    async def test_minimum_two_keyframes(self, strategy, video_request, tmp_path):
        """Legacy preview interval no longer multiplies draft GPU work."""
        config = DraftWorkflowConfig(
            preview_video_duration_seconds=3,
            preview_keyframe_interval_seconds=60,  # interval > duration
            store_dir=str(tmp_path / "drafts"),
        )
        result = await strategy.generate_draft(video_request, config)
        final = await _await_generating(strategy, result.draft_id)

        assert len(final.previews) == 1

    @pytest.mark.asyncio
    async def test_explicit_keyframe_count_override(
        self, strategy, video_request, default_config
    ):
        """Legacy keyframe count is recorded but does not multiply previews."""
        result = await strategy.generate_draft(
            video_request, default_config, keyframe_count=10
        )
        final = await _await_generating(strategy, result.draft_id)

        assert len(final.previews) == 1
        assert final.generation_params["explicit_keyframe_count"] == 10

    @pytest.mark.asyncio
    async def test_explicit_keyframe_count_minimum_two(
        self, strategy, video_request, default_config
    ):
        """A legacy count of one still produces exactly one approved keyframe."""
        result = await strategy.generate_draft(
            video_request, default_config, keyframe_count=1
        )
        final = await _await_generating(strategy, result.draft_id)

        assert len(final.previews) == 1

    @pytest.mark.asyncio
    async def test_generation_params_records_media_type(
        self, strategy, video_request, default_config
    ):
        """generation_params should record media_type as 'video'."""
        result = await strategy.generate_draft(video_request, default_config)

        assert result.generation_params["media_type"] == "video"


# ===================================================================
# Test: confirm_draft
# ===================================================================


class TestConfirmDraft:
    """Tests for draft confirmation and upscaling."""

    @pytest.mark.asyncio
    async def test_confirm_returns_upscale_result(
        self, strategy, image_request, default_config
    ):
        """Confirming a pending draft should return UpscaleResult."""
        draft = await strategy.generate_draft(image_request, default_config)
        await _await_generating(strategy, draft.draft_id)
        result = await strategy.confirm_draft(draft.draft_id)

        assert isinstance(result, UpscaleResult)
        assert result.draft_id == draft.draft_id

    @pytest.mark.asyncio
    async def test_confirm_target_resolution(
        self, strategy, image_request, default_config
    ):
        """ComfyUI refinement returns the requested bounded resolution."""
        draft = await strategy.generate_draft(image_request, default_config)
        await _await_generating(strategy, draft.draft_id)
        result = await strategy.confirm_draft(draft.draft_id)

        # target_resolution 必须是有效的 (w, h) 正整数对
        assert isinstance(result.target_resolution, tuple)
        assert len(result.target_resolution) == 2
        w, h = result.target_resolution
        assert isinstance(w, int) and isinstance(h, int)
        assert w > 0 and h > 0
        assert result.target_resolution == (1920, 1080)

    @pytest.mark.asyncio
    async def test_confirm_algorithm_from_config(
        self, strategy, image_request, default_config
    ):
        """Result identifies the versioned ComfyUI workflow."""
        draft = await strategy.generate_draft(image_request, default_config)
        await _await_generating(strategy, draft.draft_id)
        result = await strategy.confirm_draft(draft.draft_id)

        assert result.algorithm_used == "comfyui:image-v1"

    @pytest.mark.asyncio
    async def test_confirm_updates_status(
        self, strategy, image_request, default_config
    ):
        """After confirmation, draft status should be 'confirmed'."""
        draft = await strategy.generate_draft(image_request, default_config)
        await _await_generating(strategy, draft.draft_id)
        await strategy.confirm_draft(draft.draft_id)

        stored = await strategy.get_draft(draft.draft_id)
        assert stored is not None
        assert stored.status == DRAFT_STATUS_COMPLETED

    @pytest.mark.asyncio
    async def test_claimed_refining_draft_keeps_persisted_preview(
        self, strategy, image_request, default_config
    ):
        """Confirm claim must not hide the persisted preview from refinement."""
        draft = await strategy.generate_draft(image_request, default_config)
        pending = await _await_generating(strategy, draft.draft_id)
        assert pending.status == DRAFT_STATUS_PENDING
        assert pending.previews

        claimed, ok = await strategy._claim_draft_confirmation(draft.draft_id)

        assert ok is True
        assert claimed is not None
        assert claimed.status == DRAFT_STATUS_REFINING
        assert claimed.previews == pending.previews

    @pytest.mark.asyncio
    async def test_confirm_nonexistent_raises_error(self, strategy):
        """Confirming a nonexistent draft should raise DraftWorkflowError."""
        with pytest.raises(DraftWorkflowError, match="not found"):
            await strategy.confirm_draft("nonexistent_draft_id")

    @pytest.mark.asyncio
    async def test_confirm_already_confirmed_raises_error(
        self, strategy, image_request, default_config
    ):
        """Confirming an already confirmed draft returns the persisted result."""
        draft = await strategy.generate_draft(image_request, default_config)
        await _await_generating(strategy, draft.draft_id)
        first = await strategy.confirm_draft(draft.draft_id)

        second = await strategy.confirm_draft(draft.draft_id)
        assert isinstance(second, UpscaleResult)
        assert second.draft_id == first.draft_id

    @pytest.mark.asyncio
    async def test_confirm_respects_max_resolution(self, strategy, default_config):
        """Target resolution should not exceed max_target_resolution."""
        request = GenerationRequest(
            prompt="A landscape",
            target_resolution=(8000, 8000),  # exceeds max
        )
        draft = await strategy.generate_draft(request, default_config)
        await _await_generating(strategy, draft.draft_id)
        result = await strategy.confirm_draft(draft.draft_id)

        assert result.target_resolution[0] <= 4096
        assert result.target_resolution[1] <= 4096


# ===================================================================
# Test: reject_draft
# ===================================================================


class TestRejectDraft:
    """Tests for draft rejection and regeneration."""

    @pytest.mark.asyncio
    async def test_reject_generates_new_draft(
        self, strategy, image_request, default_config
    ):
        """Rejecting a draft should produce a new DraftResult."""
        draft = await strategy.generate_draft(image_request, default_config)
        await _await_generating(strategy, draft.draft_id)
        new_draft = await strategy.reject_draft(draft.draft_id)

        assert isinstance(new_draft, DraftResult)
        assert new_draft.draft_id != draft.draft_id

    @pytest.mark.asyncio
    async def test_reject_increments_attempt(
        self, strategy, image_request, default_config
    ):
        """Rejection should increment the attempt number."""
        draft = await strategy.generate_draft(image_request, default_config)
        await _await_generating(strategy, draft.draft_id)
        assert draft.attempt_number == 1

        new_draft = await strategy.reject_draft(draft.draft_id)
        assert new_draft.attempt_number == 2

    @pytest.mark.asyncio
    async def test_reject_deletes_old_draft(
        self, strategy, image_request, default_config
    ):
        """Rejected draft should be deleted (not cached)."""
        draft = await strategy.generate_draft(image_request, default_config)
        await _await_generating(strategy, draft.draft_id)
        await strategy.reject_draft(draft.draft_id)

        # Old draft should be gone
        old = await strategy.get_draft(draft.draft_id)
        assert old is None

    @pytest.mark.asyncio
    async def test_reject_nonexistent_raises_error(self, strategy):
        """Rejecting a nonexistent draft should raise DraftWorkflowError."""
        with pytest.raises(DraftWorkflowError, match="not found"):
            await strategy.reject_draft("nonexistent_id")

    @pytest.mark.asyncio
    async def test_reject_limit_reached_raises_error(
        self, strategy, image_request, tmp_path
    ):
        """Should raise error when max_regeneration_attempts reached."""
        config = DraftWorkflowConfig(max_regeneration_attempts=3, store_dir=str(tmp_path / "drafts"))
        draft = await strategy.generate_draft(image_request, config)
        await _await_generating(strategy, draft.draft_id)

        # Reject twice (attempt 1 -> 2, 2 -> 3)
        new_draft = await strategy.reject_draft(draft.draft_id)
        await _await_generating(strategy, new_draft.draft_id)
        new_draft2 = await strategy.reject_draft(new_draft.draft_id)
        await _await_generating(strategy, new_draft2.draft_id)

        # Third rejection should fail (attempt_number == 3 == max)
        with pytest.raises(DraftWorkflowError, match="Regeneration limit"):
            await strategy.reject_draft(new_draft2.draft_id)

    @pytest.mark.asyncio
    async def test_reject_limit_preserves_last_draft(
        self, strategy, image_request, tmp_path
    ):
        """When limit is reached, the last draft should still be retrievable."""
        config = DraftWorkflowConfig(max_regeneration_attempts=2, store_dir=str(tmp_path / "drafts"))
        draft = await strategy.generate_draft(image_request, config)
        await _await_generating(strategy, draft.draft_id)
        new_draft = await strategy.reject_draft(draft.draft_id)
        await _await_generating(strategy, new_draft.draft_id)

        # This rejection should fail
        with pytest.raises(DraftWorkflowError):
            await strategy.reject_draft(new_draft.draft_id)

        # But the most recent draft is preserved
        preserved = await strategy.get_draft(new_draft.draft_id)
        assert preserved is not None
        assert preserved.draft_id == new_draft.draft_id


# ===================================================================
# Test: get_draft
# ===================================================================


class TestGetDraft:
    """Tests for draft retrieval."""

    @pytest.mark.asyncio
    async def test_get_existing_draft(self, strategy, image_request, default_config):
        """Should retrieve an existing draft by ID (pending after async gen)."""
        draft = await strategy.generate_draft(image_request, default_config)
        await _await_generating(strategy, draft.draft_id)
        retrieved = await strategy.get_draft(draft.draft_id)

        assert retrieved is not None
        assert retrieved.draft_id == draft.draft_id
        assert retrieved.status == DRAFT_STATUS_PENDING

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, strategy):
        """Should return None for nonexistent draft."""
        result = await strategy.get_draft("does_not_exist")
        assert result is None


@pytest.mark.asyncio
async def test_video_id_persists_through_store_load(strategy, video_request, default_config):
    """DraftResult.video_id 应能通过 _store_draft / _load_draft 往返。"""
    result = await strategy.generate_draft(video_request, default_config)
    draft = await _await_generating(strategy, result.draft_id)
    draft.video_id = "vid_abc123"
    await strategy._store_draft(draft, ttl_seconds=60)
    reloaded = await strategy._load_draft(draft.draft_id)
    assert reloaded is not None
    assert reloaded.video_id == "vid_abc123"


def test_video_submit_result_dataclass():
    """VideoSubmitResult 字段与默认值。"""
    r = VideoSubmitResult(draft_id="d1", video_id="vid_x")
    assert r.draft_id == "d1"
    assert r.video_id == "vid_x"
    assert r.status == "generating"


@pytest.mark.asyncio
async def test_confirm_video_draft_fails_closed_without_comfy_video_workflow(strategy, video_request, default_config):
    """图片阶段不能把视频确认静默回退到 provider。"""
    strategy._comfyui_config.video_enabled = False
    result = await strategy.generate_draft(video_request, default_config)
    draft = await _await_generating(strategy, result.draft_id)
    assert draft.media_type == "video"

    from unittest.mock import AsyncMock
    strategy._litellm_bridge = AsyncMock()
    with pytest.raises(DraftWorkflowError, match="comfyui_video_not_enabled"):
        await strategy.confirm_draft(draft.draft_id)
    strategy._litellm_bridge._do_video_generation.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_video_draft_never_submits_provider_under_concurrency(strategy, video_request, default_config):
    """并发视频确认也不能调用 provider。"""
    strategy._comfyui_config.video_enabled = False
    result = await strategy.generate_draft(video_request, default_config)
    draft = await _await_generating(strategy, result.draft_id)

    from unittest.mock import AsyncMock
    strategy._litellm_bridge = AsyncMock()
    results = await asyncio.gather(
        strategy.confirm_draft(draft.draft_id),
        strategy.confirm_draft(draft.draft_id),
        return_exceptions=True,
    )
    assert all(isinstance(item, DraftWorkflowError) for item in results)
    strategy._litellm_bridge._do_video_generation.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_image_draft_still_returns_upscale_result(strategy, image_request, default_config):
    """图片草稿确认仍走放大路径,返回 UpscaleResult(回归保护)。"""
    result = await strategy.generate_draft(image_request, default_config)
    draft = await _await_generating(strategy, result.draft_id)
    assert draft.media_type == "image"

    from unittest.mock import AsyncMock
    strategy._litellm_bridge = AsyncMock()  # 图片路径不应调 _do_video_generation

    out = await strategy.confirm_draft(draft.draft_id)
    assert isinstance(out, UpscaleResult)
    assert not isinstance(out, VideoSubmitResult)
    strategy._litellm_bridge._do_video_generation.assert_not_called()


@pytest.mark.asyncio
async def test_sync_marks_stale_running_draft_without_prompt_failed(strategy, image_request):
    """A lost background worker must not leave the browser stuck at 10%."""
    draft = DraftResult(
        draft_id="stale-running",
        previews=[],
        generation_params={"trace_id": "trace-stale"},
        created_at=time.time() - 120,
        expires_at=time.time() + 3600,
        attempt_number=1,
        max_attempts=5,
        status=DRAFT_STATUS_RUNNING,
        media_type="image",
        session_id="sess-stale",
        progress=0.1,
        stage="running",
    )
    await strategy._store_draft(draft, ttl_seconds=3600)

    synced = await strategy.sync_draft_runtime_state(draft.draft_id)

    assert synced is not None
    assert synced.status == DRAFT_STATUS_FAILED
    assert synced.error == "draft_worker_lost"
    assert synced.progress == 0.0
    reloaded = await strategy.get_draft(draft.draft_id)
    assert reloaded is not None
    assert reloaded.status == DRAFT_STATUS_FAILED
    assert reloaded.error == "draft_worker_lost"


@pytest.mark.asyncio
async def test_sync_keeps_running_draft_when_comfyui_state_check_is_transient(
    strategy,
):
    """Busy ComfyUI status endpoints must not turn preview polling into 500/lost."""
    draft = DraftResult(
        draft_id="running-comfyui-busy",
        previews=[],
        generation_params={"trace_id": "trace-comfyui-busy"},
        created_at=time.time() - 120,
        expires_at=time.time() + 3600,
        attempt_number=1,
        max_attempts=5,
        status=DRAFT_STATUS_RUNNING,
        media_type="image",
        session_id="sess-comfyui-busy",
        progress=0.3125,
        stage="sampling 3/12",
        comfy_prompt_id="prompt-busy",
    )
    await strategy._store_draft(draft, ttl_seconds=3600)
    strategy._get_comfy_prompt_state = AsyncMock(side_effect=TimeoutError)

    synced = await strategy.sync_draft_runtime_state(draft.draft_id)

    assert synced is not None
    assert synced.status == DRAFT_STATUS_RUNNING
    assert synced.progress == 0.3125
    assert synced.stage == "sampling 3/12"
    assert synced.error is None


@pytest.mark.asyncio
async def test_shutdown_cancels_and_awaits_owned_background_tasks(strategy):
    started = asyncio.Event()

    async def never_finishes():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(never_finishes())
    strategy._bg_tasks.add(task)
    task.add_done_callback(strategy._bg_tasks.discard)
    await started.wait()

    await strategy.shutdown()

    assert task.cancelled()
    assert strategy._bg_tasks == set()


@pytest.mark.asyncio
async def test_delete_session_rejects_wrong_owner_without_deleting(strategy):
    draft = DraftResult(
        draft_id="draft-owned",
        previews=[b"preview"],
        generation_params={},
        created_at=time.time(),
        expires_at=time.time() + 60,
        attempt_number=1,
        max_attempts=3,
        status=DRAFT_STATUS_PENDING,
        media_type="image",
        session_id="session-owned",
        user_id="alice",
        group_id="grp-team",
    )
    await strategy._store_draft(draft, ttl_seconds=60)

    with pytest.raises(DraftWorkflowError, match="draft_session_forbidden"):
        await strategy.delete_session(
            "session-owned",
            user_id="mallory",
            group_id="grp-other",
        )

    assert await strategy.get_draft("draft-owned") is not None
    deleted = await strategy.delete_session(
        "session-owned",
        user_id="alice",
        group_id="grp-team",
    )
    assert deleted == 1
    assert await strategy.get_draft("draft-owned") is None


@pytest.mark.asyncio
async def test_delete_session_rejects_missing_owner_metadata(strategy):
    draft = DraftResult(
        draft_id="draft-legacy",
        previews=[b"preview"],
        generation_params={},
        created_at=time.time(),
        expires_at=time.time() + 60,
        attempt_number=1,
        max_attempts=3,
        status=DRAFT_STATUS_PENDING,
        media_type="image",
        session_id="session-legacy",
    )
    await strategy._store_draft(draft, ttl_seconds=60)

    with pytest.raises(DraftWorkflowError, match="draft_session_owner_unknown"):
        await strategy.delete_session(
            "session-legacy",
            user_id="alice",
            group_id="grp-team",
        )

    assert await strategy.get_draft("draft-legacy") is not None


@pytest.mark.asyncio
async def test_delete_session_skips_stray_invalid_directory(strategy):
    draft = DraftResult(
        draft_id="draft-owned",
        previews=[b"preview"],
        generation_params={},
        created_at=time.time(),
        expires_at=time.time() + 60,
        attempt_number=1,
        max_attempts=3,
        status=DRAFT_STATUS_PENDING,
        media_type="image",
        session_id="session-owned",
        user_id="alice",
        group_id="grp-team",
    )
    await strategy._store_draft(draft, ttl_seconds=60)
    stray_dir = os.path.join(strategy._store_dir, "session-owned", ".tmp")
    os.makedirs(stray_dir)

    deleted = await strategy.delete_session(
        "session-owned",
        user_id="alice",
        group_id="grp-team",
    )

    assert deleted == 1
    assert not os.path.exists(os.path.join(strategy._store_dir, "session-owned"))


@pytest.mark.asyncio
async def test_delete_session_rejects_path_traversal(strategy, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(DraftWorkflowError, match="invalid_session_id"):
        await strategy.delete_session(
            "../outside",
            user_id="alice",
            group_id="grp-team",
        )

    assert marker.read_text(encoding="utf-8") == "keep"
