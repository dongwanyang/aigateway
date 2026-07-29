"""
Media Optimization Layer (MOL)
=============================

V2 多模态媒体处理模块。
提供统一的媒体检测、分发、处理和缓存机制。
"""

from .base import MediaPipeline, MediaProcessor
from .cache import MediaCacheManager
from .config import (
    AudioPipelineConfig,
    DocumentPipelineConfig,
    GenerationConfig,
    ImagePipelineConfig,
    MediaOptimizationConfig,
    VideoPipelineConfig,
)
from .detector import ContentTypeDetector
from .mol import MediaOptimizationLayer
from .types import MediaContent, MediaType, ProcessorPhase, ProcessorResult

__all__ = [
    "AudioPipelineConfig",
    "ContentTypeDetector",
    "DocumentPipelineConfig",
    "GenerationConfig",
    "ImagePipelineConfig",
    "MediaCacheManager",
    "MediaContent",
    "MediaOptimizationConfig",
    "MediaOptimizationLayer",
    "MediaPipeline",
    "MediaProcessor",
    "MediaType",
    "ProcessorPhase",
    "ProcessorResult",
    "VideoPipelineConfig",
]
