"""
Video Preview Generator — 视频预览生成与帧插值逻辑
===================================================

用户确认视频关键帧后：
1. 生成预览视频: 默认 30 秒、8fps
2. 帧插值到目标帧率: 默认 60fps，允许范围 24-120fps

需求: 3.4
"""

from __future__ import annotations

import json
import logging
from typing import List

from aigateway_core.generation_optimization.config import DraftWorkflowConfig
from aigateway_core.generation_optimization.exceptions import DraftWorkflowError

logger = logging.getLogger(__name__)


class VideoPreviewGenerator:
    """视频预览生成器 — 从确认的关键帧生成预览视频并执行帧插值.

    工作流:
    1. 接收用户确认的关键帧 (List[bytes])
    2. 生成低帧率预览视频 (默认 30s, 8fps)
    3. 对预览视频执行帧插值，提升到目标帧率 (默认 60fps, 范围 24-120fps)

    Attributes:
        _config: Draft 工作流配置
    """

    def __init__(self, config: DraftWorkflowConfig) -> None:
        """初始化 VideoPreviewGenerator.

        Args:
            config: Draft-to-HiRes 工作流配置，包含预览视频参数
        """
        self._config = config

    async def generate_preview_video(
        self,
        keyframes: List[bytes],
        config: DraftWorkflowConfig,
    ) -> bytes:
        """从确认的关键帧生成预览视频.

        取已确认的关键帧列表，按照配置的时长和帧率生成预览视频。
        当前为占位实现，返回编码了视频元数据的 bytes。
        在生产环境中会调用实际的视频生成模型在关键帧之间进行插值生成。

        Args:
            keyframes: 已确认的关键帧图像数据列表
            config: Draft 工作流配置（允许运行时覆盖默认配置）

        Returns:
            预览视频 bytes 数据，包含编码的视频元数据（duration、fps、resolution）

        Raises:
            DraftWorkflowError: 关键帧列表为空
        """
        if not keyframes:
            raise DraftWorkflowError(
                "Cannot generate preview video: no keyframes provided"
            )

        duration = config.preview_video_duration_seconds
        fps = config.preview_video_fps
        width, height = config.draft_resolution
        total_frames = duration * fps

        # Placeholder: encode video metadata as bytes
        # In production, this would call an actual video generation model
        # to synthesize frames between keyframes
        metadata = {
            "type": "preview_video",
            "duration_seconds": duration,
            "fps": fps,
            "resolution": [width, height],
            "total_frames": total_frames,
            "keyframe_count": len(keyframes),
            "keyframe_sizes": [len(kf) for kf in keyframes],
        }

        video_data = json.dumps(metadata).encode("utf-8")

        logger.info(
            "generation_optimization.video_preview.preview_generated",
            extra={
                "duration_seconds": duration,
                "fps": fps,
                "resolution": f"{width}x{height}",
                "total_frames": total_frames,
                "keyframe_count": len(keyframes),
            },
        )

        return video_data

    async def interpolate_frames(
        self,
        video_data: bytes,
        source_fps: int,
        target_fps: int,
    ) -> bytes:
        """对低帧率视频执行帧插值，提升到目标帧率.

        验证 target_fps 在允许范围 (默认 24-120fps) 内，然后执行帧插值。
        当前为占位实现，返回编码了插值元数据的 bytes。
        在生产环境中会调用 RIFE 或类似帧插值算法。

        Args:
            video_data: 低帧率视频数据
            source_fps: 源视频帧率
            target_fps: 目标帧率

        Returns:
            插值后的视频 bytes 数据

        Raises:
            DraftWorkflowError: target_fps 超出允许范围
        """
        min_fps, max_fps = self._config.target_fps_range

        if target_fps < min_fps or target_fps > max_fps:
            raise DraftWorkflowError(
                f"Target FPS {target_fps} is out of allowed range "
                f"[{min_fps}, {max_fps}]"
            )

        if source_fps <= 0:
            raise DraftWorkflowError(
                f"Source FPS must be positive, got {source_fps}"
            )

        if target_fps <= source_fps:
            # No interpolation needed, return as-is
            logger.info(
                "generation_optimization.video_preview.no_interpolation_needed",
                extra={
                    "source_fps": source_fps,
                    "target_fps": target_fps,
                    "reason": "target_fps <= source_fps",
                },
            )
            return video_data

        # Calculate interpolation factor
        interpolation_factor = target_fps / source_fps

        # Placeholder: encode interpolated video metadata as bytes
        # In production, this would call RIFE or similar frame interpolation algorithm
        metadata = {
            "type": "interpolated_video",
            "source_fps": source_fps,
            "target_fps": target_fps,
            "interpolation_factor": round(interpolation_factor, 2),
            "source_video_size": len(video_data),
            "algorithm": "rife",  # placeholder algorithm name
        }

        interpolated_data = json.dumps(metadata).encode("utf-8")

        logger.info(
            "generation_optimization.video_preview.frames_interpolated",
            extra={
                "source_fps": source_fps,
                "target_fps": target_fps,
                "interpolation_factor": round(interpolation_factor, 2),
            },
        )

        return interpolated_data

    async def generate_and_interpolate(
        self,
        keyframes: List[bytes],
        config: DraftWorkflowConfig | None = None,
        target_fps: int | None = None,
    ) -> bytes:
        """便捷方法：生成预览视频并执行帧插值（链式操作）.

        先调用 generate_preview_video 生成低帧率预览，
        再调用 interpolate_frames 提升到目标帧率。

        Args:
            keyframes: 已确认的关键帧图像数据列表
            config: Draft 工作流配置覆盖，为 None 时使用初始化配置
            target_fps: 目标帧率覆盖，为 None 时使用配置中的 target_fps

        Returns:
            帧插值后的视频 bytes 数据

        Raises:
            DraftWorkflowError: 关键帧为空或目标帧率超出范围
        """
        effective_config = config if config is not None else self._config
        effective_target_fps = (
            target_fps if target_fps is not None else effective_config.target_fps
        )

        # Step 1: Generate preview video at low fps
        preview_video = await self.generate_preview_video(
            keyframes=keyframes,
            config=effective_config,
        )

        # Step 2: Interpolate frames to target fps
        source_fps = effective_config.preview_video_fps
        interpolated_video = await self.interpolate_frames(
            video_data=preview_video,
            source_fps=source_fps,
            target_fps=effective_target_fps,
        )

        logger.info(
            "generation_optimization.video_preview.generate_and_interpolate_complete",
            extra={
                "keyframe_count": len(keyframes),
                "preview_fps": source_fps,
                "target_fps": effective_target_fps,
            },
        )

        return interpolated_video
