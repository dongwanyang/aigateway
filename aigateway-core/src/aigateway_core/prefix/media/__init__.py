"""
Media Optimization Layer (MOL)
=============================

V2 多模态媒体处理模块。
提供统一的媒体检测、分发、处理和缓存机制。
"""

from .types import MediaType, ProcessorPhase, MediaContent, ProcessorResult
from .base import MediaProcessor, MediaPipeline
from .detector import ContentTypeDetector
from .mol import MediaOptimizationLayer
from .cache import MediaCacheManager
from .config import (
    ImagePipelineConfig,
    VideoPipelineConfig,
    AudioPipelineConfig,
    DocumentPipelineConfig,
    GenerationConfig,
    MediaOptimizationConfig,
)

__all__ = [
    "MediaType",
    "ProcessorPhase",
    "MediaContent",
    "ProcessorResult",
    "MediaProcessor",
    "MediaPipeline",
    "ContentTypeDetector",
    "MediaOptimizationLayer",
    "MediaCacheManager",
    "ImagePipelineConfig",
    "VideoPipelineConfig",
    "AudioPipelineConfig",
    "DocumentPipelineConfig",
    "GenerationConfig",
    "MediaOptimizationConfig",
]
