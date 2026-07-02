"""
Media Configuration — 多模态管线配置模型
========================================

定义各媒体管线的配置数据类。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class ImagePipelineConfig:
    """图像管线配置。"""

    max_width: int = 1920
    max_height: int = 1080
    quality: int = 85  # JPEG/WebP 质量 (1-100)
    output_format: str = "webp"  # "webp" | "jpeg" | "png"
    ocr_backend: str = "tesseract"  # "tesseract" | "paddleocr"
    ocr_languages: List[str] = field(default_factory=lambda: ["chi_sim", "eng"])
    caption_model: str = "gpt-4o"  # Vision model for captioning
    max_file_size_mb: float = 20.0


@dataclass
class VideoPipelineConfig:
    """视频管线配置。"""

    max_frames: int = 10
    frame_interval_sec: float = 5.0
    scene_detection: bool = True
    target_resolution: str = "720p"
    max_duration_sec: int = 300
    whisper_model: str = "faster-whisper"
    caption_model: str = "gpt-4o"
    language: str = "auto"
    max_file_size_mb: float = 100.0


@dataclass
class AudioPipelineConfig:
    """音频管线配置。"""

    target_format: str = "opus"
    sample_rate: int = 16000
    bitrate: str = "64k"
    whisper_model: str = "faster-whisper"
    language: str = "auto"
    max_duration_sec: int = 600
    diarization_enabled: bool = False
    max_file_size_mb: float = 50.0


@dataclass
class DocumentPipelineConfig:
    """文档管线配置。"""

    supported_formats: List[str] = field(
        default_factory=lambda: ["pdf", "docx", "xlsx", "pptx", "md", "csv", "html"]
    )
    ocr_fallback: bool = True
    chunk_size: int = 512
    chunk_overlap: int = 64
    chunking_strategy: str = "semantic"  # "semantic" | "token" | "sentence"
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    vector_dim: int = 1024
    summary_max_length: int = 500
    max_file_size_mb: float = 50.0


@dataclass
class GenerationConfig:
    """Generation Pipeline 配置。"""

    enhancement_level: str = "off"  # "off" | "light" | "aggressive"
    vision_model: str = "gpt-4o"
    default_model: str = "gpt-4o"
    max_retries: int = 3
    retry_delay_ms: int = 1000


@dataclass
class MediaOptimizationConfig:
    """Media Optimization Layer 总配置。"""

    enabled: bool = True
    image: ImagePipelineConfig = field(default_factory=ImagePipelineConfig)
    video: VideoPipelineConfig = field(default_factory=VideoPipelineConfig)
    audio: AudioPipelineConfig = field(default_factory=AudioPipelineConfig)
    document: DocumentPipelineConfig = field(default_factory=DocumentPipelineConfig)
    generation: GenerationConfig = field(default_factory=GenerationConfig)
    media_cache_ttl: int = 604800  # 7 days
    max_concurrent_processors: int = 4
