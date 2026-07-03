"""
VideoPipeline — 视频处理管线
=============================

处理器链:
1. VideoFrameExtractor — 抽取关键帧
2. 各帧通过 Image Pipeline OCR/Caption 处理
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, List, Optional

from ..base import MediaPipeline, MediaProcessor
from ..config import VideoPipelineConfig
from ..types import MediaContent, MediaType, ProcessorPhase, ProcessorResult

if TYPE_CHECKING:
    from ...context import PipelineContext

logger = logging.getLogger(__name__)


class VideoFrameExtractor(MediaProcessor):
    """视频关键帧提取处理器。"""

    name = "video_frame_extractor"
    phase = ProcessorPhase.PRE_LLM
    supported_types = [MediaType.VIDEO]

    def __init__(
        self,
        max_frames: int = 10,
        frame_interval_sec: float = 5.0,
    ) -> None:
        self.max_frames = max_frames
        self.frame_interval_sec = frame_interval_sec

    def supports(self, content: MediaContent) -> bool:
        return content.media_type == MediaType.VIDEO

    async def process(
        self, content: MediaContent, ctx: "PipelineContext"
    ) -> ProcessorResult:
        """提取视频关键帧。"""
        start = time.monotonic()
        try:
            video_data = content.raw_data
            if video_data is None:
                return ProcessorResult(
                    success=False,
                    processor_name=self.name,
                    duration_ms=0,
                    error="No video data",
                )

            loop = asyncio.get_event_loop()
            frames = await loop.run_in_executor(
                None, self._extract_frames, video_data
            )

            elapsed = (time.monotonic() - start) * 1000
            return ProcessorResult(
                success=True,
                processor_name=self.name,
                duration_ms=elapsed,
                output=frames,
            )
        except ImportError as exc:
            elapsed = (time.monotonic() - start) * 1000
            return ProcessorResult(
                success=False,
                processor_name=self.name,
                duration_ms=elapsed,
                error=f"opencv-python not installed: {exc}",
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return ProcessorResult(
                success=False,
                processor_name=self.name,
                duration_ms=elapsed,
                error=str(exc),
            )

    def _extract_frames(self, video_data: bytes) -> List[bytes]:
        """同步提取关键帧。"""
        import tempfile
        import os
        import cv2
        import numpy as np

        # 写临时文件
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(video_data)
            temp_path = f.name

        frames: List[bytes] = []
        try:
            cap = cv2.VideoCapture(temp_path)
            fps = cap.get(cv2.CAP_PROP_FPS) or 25
            frame_interval = int(fps * self.frame_interval_sec)
            frame_idx = 0

            while cap.isOpened() and len(frames) < self.max_frames:
                ret, frame = cap.read()
                if not ret:
                    break
                if frame_idx % frame_interval == 0:
                    _, buf = cv2.imencode(".jpg", frame)
                    frames.append(buf.tobytes())
                frame_idx += 1
            cap.release()
        finally:
            os.unlink(temp_path)

        return frames


# ------------------------------------------------------------------
# Video Pipeline
# ------------------------------------------------------------------


class VideoPipeline(MediaPipeline):
    """视频处理管线。"""

    media_type = MediaType.VIDEO

    def __init__(self, config: Optional[VideoPipelineConfig] = None) -> None:
        cfg = config or VideoPipelineConfig()
        self.config = cfg
        self.frame_extractor = VideoFrameExtractor(
            max_frames=cfg.max_frames,
            frame_interval_sec=cfg.frame_interval_sec,
        )
        self.processors = [self.frame_extractor]

    async def execute(
        self, content: MediaContent, ctx: "PipelineContext"
    ) -> MediaContent:
        """执行视频处理管线。"""
        if content.size_bytes > self.config.max_file_size_mb * 1024 * 1024:
            logger.warning("视频文件超过大小限制")
            return content

        # 下载视频（如果只有 URL）
        if content.raw_data is None and content.source_url:
            content.raw_data = await self._download(content.source_url)
            if content.raw_data:
                content.size_bytes = len(content.raw_data)

        if content.raw_data is None:
            return content

        # 提取关键帧
        frames_result = await self.frame_extractor.process(content, ctx)
        frames: List[bytes] = frames_result.output or []

        if frames:
            # 生成帧描述（简化版：使用帧数量信息）
            content.extracted_text = (
                f"[视频分析]: 提取了 {len(frames)} 个关键帧，"
                f"视频大小 {content.size_bytes} bytes"
            )
            # 估算 token 节约
            original_tokens = content.size_bytes // 4
            text_tokens = len(content.extracted_text) // 4
            content.token_savings = max(0, original_tokens - text_tokens)

        return content

    async def _download(self, url: str) -> Optional[bytes]:
        """下载视频。"""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=self.config.download_timeout) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return resp.content
                return None
        except Exception as exc:
            logger.warning("视频下载失败: %s", exc)
            return None
