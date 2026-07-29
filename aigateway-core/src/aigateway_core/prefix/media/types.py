"""
Media Types — 多模态媒体类型定义
================================

定义 Media Optimization Layer 使用的核心数据类型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MediaType(Enum):
    """支持的媒体类型枚举。"""

    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"


class ProcessorPhase(Enum):
    """Processor 分类（决定执行阶段）。"""

    PRE_LLM = "pre_llm"  # LLM 调用前执行（压缩、提取）
    POST_LLM = "post_llm"  # LLM 调用后执行（格式化）
    PARALLEL = "parallel"  # 可并行执行（独立处理）


@dataclass
class MediaContent:
    """媒体内容的统一抽象。"""

    media_type: MediaType
    source_url: str | None = None
    raw_data: bytes | None = None
    mime_type: str | None = None
    size_bytes: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    # 处理后产出
    extracted_text: str | None = None
    optimized_data: bytes | None = None
    embedding_vector: list[float] | None = None
    token_savings: int = 0


@dataclass
class ProcessorResult:
    """单个 Processor 的处理结果。"""

    success: bool
    processor_name: str
    duration_ms: float
    output: Any | None = None
    error: str | None = None
    token_savings: int = 0
