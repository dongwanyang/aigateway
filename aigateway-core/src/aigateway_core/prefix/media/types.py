"""
Media Types — 多模态媒体类型定义
================================

定义 Media Optimization Layer 使用的核心数据类型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


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
    source_url: Optional[str] = None
    raw_data: Optional[bytes] = None
    mime_type: Optional[str] = None
    size_bytes: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    # 处理后产出
    extracted_text: Optional[str] = None
    optimized_data: Optional[bytes] = None
    embedding_vector: Optional[List[float]] = None
    token_savings: int = 0


@dataclass
class ProcessorResult:
    """单个 Processor 的处理结果。"""

    success: bool
    processor_name: str
    duration_ms: float
    output: Optional[Any] = None
    error: Optional[str] = None
    token_savings: int = 0
