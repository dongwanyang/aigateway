"""
Tests for DraftGeneratorStrategy — 渐进式生成工作流核心逻辑
==========================================================

验证:
- 图片请求生成 512x512 预览
- 视频请求按时间间隔动态生成关键帧（默认每 5 秒一帧，最少 2 帧）
- 用户可显式指定关键帧数量覆盖间隔计算
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

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.pipelines.generation._common.config import DraftWorkflowConfig
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation._common.models import (
    DRAFT_STATUS_CONFIRMED,
    DRAFT_STATUS_GENERATING,
    DRAFT_STATUS_PENDING,
    DraftResult,
    GenerationRequest,
    UpscaleResult,
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
def strategy(default_config):
    """Create a DraftGeneratorStrategy instance with in-memory store + tmp store_dir."""
    return DraftGeneratorStrategy(config=default_config, redis_client=None)


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
        if d is not None and d.status != DRAFT_STATUS_GENERATING:
            return d
        await asyncio.sleep(0.01)
    return await strategy.get_draft(draft_id)


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
        assert result.status == DRAFT_STATUS_GENERATING
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
        assert result.status == DRAFT_STATUS_GENERATING  # 提交即 generating

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
    """Tests for video draft generation with keyframes."""

    @pytest.mark.asyncio
    async def test_default_keyframe_count(self, strategy, video_request, default_config):
        """Video: default is ceil(30/5) = 6 keyframes (after async generation)."""
        result = await strategy.generate_draft(video_request, default_config)
        final = await _await_generating(strategy, result.draft_id)

        # ceil(30 / 5) = 6 keyframes
        assert len(final.previews) == 6

    @pytest.mark.asyncio
    async def test_minimum_two_keyframes(self, strategy, video_request, tmp_path):
        """Video: at least 2 keyframes even with very long intervals."""
        config = DraftWorkflowConfig(
            preview_video_duration_seconds=3,
            preview_keyframe_interval_seconds=60,  # interval > duration
            store_dir=str(tmp_path / "drafts"),
        )
        result = await strategy.generate_draft(video_request, config)
        final = await _await_generating(strategy, result.draft_id)

        assert len(final.previews) >= 2

    @pytest.mark.asyncio
    async def test_explicit_keyframe_count_override(
        self, strategy, video_request, default_config
    ):
        """User can explicitly specify keyframe count."""
        result = await strategy.generate_draft(
            video_request, default_config, keyframe_count=10
        )
        final = await _await_generating(strategy, result.draft_id)

        assert len(final.previews) == 10

    @pytest.mark.asyncio
    async def test_explicit_keyframe_count_minimum_two(
        self, strategy, video_request, default_config
    ):
        """Explicit keyframe count is clamped to minimum 2."""
        result = await strategy.generate_draft(
            video_request, default_config, keyframe_count=1
        )
        final = await _await_generating(strategy, result.draft_id)

        assert len(final.previews) == 2

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
        """Upscale result target_resolution reflects the actual output path.

        - 无 RealESRGAN 依赖（CI/单元测试环境）：_super_resolve 返回 None，
          走 _simulate_upscale 占位，target_resolution = 配置默认 (1920, 1080)。
        - 有 RealESRGAN 依赖（Docker 生产镜像）：_super_resolve 等比放大到
          长边 4096，target_resolution = 实际输出 (4096, 4096)。
        两种环境下都应返回合理的整数分辨率，且与 result.algorithm_used 一致。
        """
        draft = await strategy.generate_draft(image_request, default_config)
        await _await_generating(strategy, draft.draft_id)
        result = await strategy.confirm_draft(draft.draft_id)

        # target_resolution 必须是有效的 (w, h) 正整数对
        assert isinstance(result.target_resolution, tuple)
        assert len(result.target_resolution) == 2
        w, h = result.target_resolution
        assert isinstance(w, int) and isinstance(h, int)
        assert w > 0 and h > 0
        # 占位路径返回配置的 (1920, 1080)；真实超分路径返回 (4096, 4096)
        assert result.target_resolution in ((1920, 1080), (4096, 4096))

    @pytest.mark.asyncio
    async def test_confirm_algorithm_from_config(
        self, strategy, image_request, default_config
    ):
        """Upscale result should use the configured algorithm."""
        draft = await strategy.generate_draft(image_request, default_config)
        await _await_generating(strategy, draft.draft_id)
        result = await strategy.confirm_draft(draft.draft_id)

        assert result.algorithm_used == "real-esrgan"

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
        assert stored.status == DRAFT_STATUS_CONFIRMED

    @pytest.mark.asyncio
    async def test_confirm_nonexistent_raises_error(self, strategy):
        """Confirming a nonexistent draft should raise DraftWorkflowError."""
        with pytest.raises(DraftWorkflowError, match="not found"):
            await strategy.confirm_draft("nonexistent_draft_id")

    @pytest.mark.asyncio
    async def test_confirm_already_confirmed_raises_error(
        self, strategy, image_request, default_config
    ):
        """Confirming an already confirmed draft should raise error."""
        draft = await strategy.generate_draft(image_request, default_config)
        await _await_generating(strategy, draft.draft_id)
        await strategy.confirm_draft(draft.draft_id)

        with pytest.raises(DraftWorkflowError, match="cannot be confirmed"):
            await strategy.confirm_draft(draft.draft_id)

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
        new_draft2 = await strategy.reject_draft(new_draft.draft_id)

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
