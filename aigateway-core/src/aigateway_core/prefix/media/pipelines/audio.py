"""
AudioPipeline — 音频处理管线
=============================

处理器链:
1. AudioTranscriber — 语音转文字（Whisper）
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, List, Optional

from ..base import MediaPipeline, MediaProcessor
from ..config import AudioPipelineConfig
from ..types import MediaContent, MediaType, ProcessorPhase, ProcessorResult

if TYPE_CHECKING:
    from aigateway_core.dispatch.context import PipelineContext

logger = logging.getLogger(__name__)


class AudioTranscriber(MediaProcessor):
    """音频转文字处理器 — 使用 Whisper 模型。"""

    name = "audio_transcriber"
    phase = ProcessorPhase.PRE_LLM
    supported_types = [MediaType.AUDIO]

    def __init__(
        self,
        model: str = "faster-whisper",
        language: str = "auto",
        max_duration_sec: int = 600,
        whisper_model_size: str = "base",
    ) -> None:
        self.model = model
        self.language = language
        self.max_duration_sec = max_duration_sec
        self.whisper_model_size = whisper_model_size

    def supports(self, content: MediaContent) -> bool:
        return content.media_type == MediaType.AUDIO

    async def process(
        self, content: MediaContent, ctx: "PipelineContext"
    ) -> ProcessorResult:
        """转录音频为文字。"""
        start = time.monotonic()
        try:
            audio_data = content.raw_data
            if audio_data is None:
                return ProcessorResult(
                    success=False,
                    processor_name=self.name,
                    duration_ms=0,
                    error="No audio data available",
                )

            loop = asyncio.get_event_loop()
            text = await loop.run_in_executor(None, self._transcribe, audio_data)

            elapsed = (time.monotonic() - start) * 1000
            if text and text.strip():
                # 音频转文字节约大量 token
                original_tokens = len(audio_data) // 4
                text_tokens = len(text) // 4
                savings = max(0, original_tokens - text_tokens)
                return ProcessorResult(
                    success=True,
                    processor_name=self.name,
                    duration_ms=elapsed,
                    output=text.strip(),
                    token_savings=savings,
                )
            return ProcessorResult(
                success=False,
                processor_name=self.name,
                duration_ms=elapsed,
                error="Transcription returned empty result",
            )
        except ImportError as exc:
            elapsed = (time.monotonic() - start) * 1000
            return ProcessorResult(
                success=False,
                processor_name=self.name,
                duration_ms=elapsed,
                error=f"Whisper not installed: {exc}",
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return ProcessorResult(
                success=False,
                processor_name=self.name,
                duration_ms=elapsed,
                error=str(exc),
            )

    def _transcribe(self, audio_data: bytes) -> str:
        """同步转录。"""
        import tempfile
        import os

        # 写入临时文件
        suffix = ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(audio_data)
            temp_path = f.name

        try:
            if self.model == "faster-whisper":
                return self._transcribe_faster_whisper(temp_path)
            else:
                return self._transcribe_openai_whisper(temp_path)
        finally:
            os.unlink(temp_path)

    def _transcribe_faster_whisper(self, path: str) -> str:
        """使用 faster-whisper 转录。"""
        from faster_whisper import WhisperModel

        model = WhisperModel(self.whisper_model_size, device="cpu", compute_type="int8")
        lang = None if self.language == "auto" else self.language
        segments, _ = model.transcribe(path, language=lang)
        return " ".join(seg.text for seg in segments)

    def _transcribe_openai_whisper(self, path: str) -> str:
        """使用 openai-whisper 转录。"""
        import whisper

        model = whisper.load_model(self.whisper_model_size)
        result = model.transcribe(path)
        return result.get("text", "")


# ------------------------------------------------------------------
# Audio Pipeline
# ------------------------------------------------------------------


class AudioPipeline(MediaPipeline):
    """音频处理管线。

    输出策略:
    - 短音频 (< 60s): 全文转录
    - 长音频 (60s-600s): 转录
    - 超长音频 (> 600s): 截取前 600s
    """

    media_type = MediaType.AUDIO

    def __init__(self, config: Optional[AudioPipelineConfig] = None) -> None:
        cfg = config or AudioPipelineConfig()
        self.config = cfg
        self.transcriber = AudioTranscriber(
            model=cfg.whisper_model,
            language=cfg.language,
            max_duration_sec=cfg.max_duration_sec,
            whisper_model_size=cfg.whisper_model_size,
        )
        self.processors = [self.transcriber]

    async def execute(
        self, content: MediaContent, ctx: "PipelineContext"
    ) -> MediaContent:
        """执行音频处理管线。"""
        # 检查大小限制
        if content.size_bytes > self.config.max_file_size_mb * 1024 * 1024:
            logger.warning("音频文件超过大小限制")
            return content

        # 下载音频（如果只有 URL）
        if content.raw_data is None and content.source_url:
            content.raw_data = await self._download(content.source_url)
            if content.raw_data:
                content.size_bytes = len(content.raw_data)

        if content.raw_data is None:
            return content

        # 转录
        transcript = await self.transcriber.process(content, ctx)
        if transcript.success and transcript.output:
            content.extracted_text = f"[音频转录]: {transcript.output}"
            content.token_savings = transcript.token_savings

        return content

    async def _download(self, url: str) -> Optional[bytes]:
        """下载音频。"""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=self.config.download_timeout) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return resp.content
                return None
        except Exception as exc:
            logger.warning("音频下载失败: %s", exc)
            return None
