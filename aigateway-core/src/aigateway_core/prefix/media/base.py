"""
Media Base Classes — 媒体处理器和管线基类
=========================================

定义 MediaProcessor 和 MediaPipeline 的抽象基类。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, List

from .types import MediaContent, MediaType, ProcessorPhase, ProcessorResult

if TYPE_CHECKING:
    from aigateway_core.dispatch.context import PipelineContext


class MediaProcessor(ABC):
    """媒体处理器基类 — 所有 Pipeline 中的处理单元实现此接口。"""

    name: str
    phase: ProcessorPhase
    supported_types: List[MediaType]

    @abstractmethod
    async def process(
        self, content: MediaContent, ctx: "PipelineContext"
    ) -> ProcessorResult:
        """处理媒体内容。

        Args:
            content: 待处理的媒体内容。
            ctx: Pipeline 上下文（可读取配置、写入结果）。

        Returns:
            处理结果，包含成功/失败状态和输出。
        """
        ...

    @abstractmethod
    def supports(self, content: MediaContent) -> bool:
        """判断此 Processor 是否支持给定媒体内容。"""
        ...


class MediaPipeline(ABC):
    """媒体处理管线基类 — 每种媒体类型有一个 Pipeline。"""

    media_type: MediaType
    processors: List[MediaProcessor]

    @abstractmethod
    async def execute(
        self, content: MediaContent, ctx: "PipelineContext"
    ) -> MediaContent:
        """执行该媒体类型的完整处理流程。

        Args:
            content: 原始媒体内容。
            ctx: Pipeline 上下文。

        Returns:
            处理后的媒体内容（含提取文本、优化数据等）。
        """
        ...
