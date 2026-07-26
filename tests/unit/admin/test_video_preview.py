"""
Tests for VideoPreviewGenerator — 视频预览生成与帧插值逻辑
==========================================================

验证:
- 关键帧确认后生成预览视频: 默认 30 秒、8fps
- 帧插值到目标帧率: 默认 60fps，范围 24-120fps
- target_fps 范围校验
- generate_and_interpolate 链式操作

需求: 3.4
"""

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.pipelines.generation._common.config import DraftWorkflowConfig
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation.token.video_preview import (
    VideoPreviewGenerator,
)


@pytest.fixture
def default_config():
    """Default Draft workflow config."""
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
    )


@pytest.fixture
def generator(default_config):
    """Create a VideoPreviewGenerator instance."""
    return VideoPreviewGenerator(config=default_config)


@pytest.fixture
def sample_keyframes():
    """Create sample keyframe data."""
    return [
        b"KEYFRAME_0_data_placeholder",
        b"KEYFRAME_1_data_placeholder",
        b"KEYFRAME_2_data_placeholder",
        b"KEYFRAME_3_data_placeholder",
        b"KEYFRAME_4_data_placeholder",
        b"KEYFRAME_5_data_placeholder",
    ]


# ==================================================================
# generate_preview_video 测试
# ==================================================================


class TestGeneratePreviewVideo:
    """Tests for generate_preview_video method."""

    @pytest.mark.asyncio
    async def test_generates_preview_with_correct_metadata(
        self, generator, default_config, sample_keyframes
    ):
        """Preview video contains correct duration, fps, and resolution metadata."""
        result = await generator.generate_preview_video(
            keyframes=sample_keyframes,
            config=default_config,
        )

        metadata = json.loads(result.decode("utf-8"))
        assert metadata["type"] == "preview_video"
        assert metadata["duration_seconds"] == 30
        assert metadata["fps"] == 8
        assert metadata["resolution"] == [512, 512]
        assert metadata["total_frames"] == 30 * 8  # 240
        assert metadata["keyframe_count"] == 6

    @pytest.mark.asyncio
    async def test_respects_custom_config(self, sample_keyframes):
        """Custom config parameters are reflected in the output metadata."""
        custom_config = DraftWorkflowConfig(
            preview_video_duration_seconds=60,
            preview_video_fps=12,
            draft_resolution=(720, 480),
        )
        gen = VideoPreviewGenerator(config=custom_config)

        result = await gen.generate_preview_video(
            keyframes=sample_keyframes,
            config=custom_config,
        )

        metadata = json.loads(result.decode("utf-8"))
        assert metadata["duration_seconds"] == 60
        assert metadata["fps"] == 12
        assert metadata["resolution"] == [720, 480]
        assert metadata["total_frames"] == 60 * 12

    @pytest.mark.asyncio
    async def test_records_keyframe_sizes(self, generator, default_config):
        """Preview metadata includes size of each input keyframe."""
        keyframes = [b"abc", b"defgh", b"ij"]
        result = await generator.generate_preview_video(
            keyframes=keyframes,
            config=default_config,
        )

        metadata = json.loads(result.decode("utf-8"))
        assert metadata["keyframe_sizes"] == [3, 5, 2]

    @pytest.mark.asyncio
    async def test_raises_on_empty_keyframes(self, generator, default_config):
        """Raises DraftWorkflowError when keyframes list is empty."""
        with pytest.raises(DraftWorkflowError, match="no keyframes provided"):
            await generator.generate_preview_video(
                keyframes=[],
                config=default_config,
            )

    @pytest.mark.asyncio
    async def test_single_keyframe_succeeds(self, generator, default_config):
        """Can generate preview with only one keyframe."""
        result = await generator.generate_preview_video(
            keyframes=[b"single_frame"],
            config=default_config,
        )

        metadata = json.loads(result.decode("utf-8"))
        assert metadata["keyframe_count"] == 1


# ==================================================================
# interpolate_frames 测试
# ==================================================================


class TestInterpolateFrames:
    """Tests for interpolate_frames method."""

    @pytest.mark.asyncio
    async def test_interpolates_to_target_fps(self, generator):
        """Interpolation produces output with correct target fps metadata."""
        source_video = b"source_video_data_placeholder"
        result = await generator.interpolate_frames(
            video_data=source_video,
            source_fps=8,
            target_fps=60,
        )

        metadata = json.loads(result.decode("utf-8"))
        assert metadata["type"] == "interpolated_video"
        assert metadata["source_fps"] == 8
        assert metadata["target_fps"] == 60
        assert metadata["interpolation_factor"] == 7.5
        assert metadata["source_video_size"] == len(source_video)

    @pytest.mark.asyncio
    async def test_rejects_target_fps_below_minimum(self, generator):
        """Raises DraftWorkflowError when target_fps < 24."""
        with pytest.raises(DraftWorkflowError, match="out of allowed range"):
            await generator.interpolate_frames(
                video_data=b"data",
                source_fps=8,
                target_fps=23,
            )

    @pytest.mark.asyncio
    async def test_rejects_target_fps_above_maximum(self, generator):
        """Raises DraftWorkflowError when target_fps > 120."""
        with pytest.raises(DraftWorkflowError, match="out of allowed range"):
            await generator.interpolate_frames(
                video_data=b"data",
                source_fps=8,
                target_fps=121,
            )

    @pytest.mark.asyncio
    async def test_accepts_boundary_fps_24(self, generator):
        """target_fps=24 (minimum boundary) is accepted."""
        result = await generator.interpolate_frames(
            video_data=b"data",
            source_fps=8,
            target_fps=24,
        )

        metadata = json.loads(result.decode("utf-8"))
        assert metadata["target_fps"] == 24

    @pytest.mark.asyncio
    async def test_accepts_boundary_fps_120(self, generator):
        """target_fps=120 (maximum boundary) is accepted."""
        result = await generator.interpolate_frames(
            video_data=b"data",
            source_fps=8,
            target_fps=120,
        )

        metadata = json.loads(result.decode("utf-8"))
        assert metadata["target_fps"] == 120

    @pytest.mark.asyncio
    async def test_no_interpolation_when_target_leq_source(self, generator):
        """Returns original data when target_fps <= source_fps."""
        source_video = b"original_video_data"
        result = await generator.interpolate_frames(
            video_data=source_video,
            source_fps=60,
            target_fps=60,
        )
        assert result == source_video

    @pytest.mark.asyncio
    async def test_no_interpolation_when_target_less_than_source(self, generator):
        """Returns original data when target_fps < source_fps."""
        source_video = b"original_video_data"
        result = await generator.interpolate_frames(
            video_data=source_video,
            source_fps=60,
            target_fps=30,
        )
        assert result == source_video

    @pytest.mark.asyncio
    async def test_rejects_zero_source_fps(self, generator):
        """Raises DraftWorkflowError when source_fps is 0."""
        with pytest.raises(DraftWorkflowError, match="Source FPS must be positive"):
            await generator.interpolate_frames(
                video_data=b"data",
                source_fps=0,
                target_fps=60,
            )

    @pytest.mark.asyncio
    async def test_rejects_negative_source_fps(self, generator):
        """Raises DraftWorkflowError when source_fps is negative."""
        with pytest.raises(DraftWorkflowError, match="Source FPS must be positive"):
            await generator.interpolate_frames(
                video_data=b"data",
                source_fps=-5,
                target_fps=60,
            )

    @pytest.mark.asyncio
    async def test_custom_fps_range(self):
        """Custom target_fps_range is respected."""
        config = DraftWorkflowConfig(target_fps_range=(30, 90))
        gen = VideoPreviewGenerator(config=config)

        # 29 is below custom minimum of 30
        with pytest.raises(DraftWorkflowError, match="out of allowed range"):
            await gen.interpolate_frames(
                video_data=b"data",
                source_fps=8,
                target_fps=29,
            )

        # 91 is above custom maximum of 90
        with pytest.raises(DraftWorkflowError, match="out of allowed range"):
            await gen.interpolate_frames(
                video_data=b"data",
                source_fps=8,
                target_fps=91,
            )

        # 30 and 90 are both within custom range
        result = await gen.interpolate_frames(
            video_data=b"data", source_fps=8, target_fps=30
        )
        metadata = json.loads(result.decode("utf-8"))
        assert metadata["target_fps"] == 30


# ==================================================================
# generate_and_interpolate 测试
# ==================================================================


class TestGenerateAndInterpolate:
    """Tests for generate_and_interpolate convenience method."""

    @pytest.mark.asyncio
    async def test_chains_preview_and_interpolation(
        self, generator, sample_keyframes
    ):
        """Chains generate_preview_video and interpolate_frames correctly."""
        result = await generator.generate_and_interpolate(
            keyframes=sample_keyframes,
        )

        # Result should be interpolated video metadata
        metadata = json.loads(result.decode("utf-8"))
        assert metadata["type"] == "interpolated_video"
        assert metadata["source_fps"] == 8
        assert metadata["target_fps"] == 60

    @pytest.mark.asyncio
    async def test_uses_custom_config(self, sample_keyframes):
        """Custom config overrides default values in the chain."""
        custom_config = DraftWorkflowConfig(
            preview_video_duration_seconds=15,
            preview_video_fps=4,
            target_fps=48,
            target_fps_range=(24, 120),
            draft_resolution=(256, 256),
        )
        gen = VideoPreviewGenerator(config=custom_config)

        result = await gen.generate_and_interpolate(
            keyframes=sample_keyframes,
            config=custom_config,
        )

        metadata = json.loads(result.decode("utf-8"))
        assert metadata["source_fps"] == 4
        assert metadata["target_fps"] == 48

    @pytest.mark.asyncio
    async def test_uses_target_fps_override(self, generator, sample_keyframes):
        """target_fps parameter overrides config value."""
        result = await generator.generate_and_interpolate(
            keyframes=sample_keyframes,
            target_fps=90,
        )

        metadata = json.loads(result.decode("utf-8"))
        assert metadata["target_fps"] == 90

    @pytest.mark.asyncio
    async def test_raises_on_empty_keyframes(self, generator):
        """Raises DraftWorkflowError when keyframes list is empty."""
        with pytest.raises(DraftWorkflowError, match="no keyframes provided"):
            await generator.generate_and_interpolate(keyframes=[])

    @pytest.mark.asyncio
    async def test_raises_on_invalid_target_fps(
        self, generator, sample_keyframes
    ):
        """Raises DraftWorkflowError when target_fps is out of range."""
        with pytest.raises(DraftWorkflowError, match="out of allowed range"):
            await generator.generate_and_interpolate(
                keyframes=sample_keyframes,
                target_fps=200,
            )

    @pytest.mark.asyncio
    async def test_uses_initial_config_when_none_passed(self, sample_keyframes):
        """Uses init config when no config override is provided."""
        init_config = DraftWorkflowConfig(
            preview_video_fps=10,
            target_fps=50,
            target_fps_range=(24, 120),
        )
        gen = VideoPreviewGenerator(config=init_config)

        result = await gen.generate_and_interpolate(
            keyframes=sample_keyframes,
        )

        metadata = json.loads(result.decode("utf-8"))
        assert metadata["source_fps"] == 10
        assert metadata["target_fps"] == 50
