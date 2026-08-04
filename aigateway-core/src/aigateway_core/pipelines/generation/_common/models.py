"""
数据模型 — 生成优化层核心数据结构
===================================
"""
from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from aigateway_core.prefix.media.types import MediaContent


@dataclass
class GenerationRequest:
    """生成请求 — 包含优化所需的全部信息."""

    prompt: str
    source_prompt: str | None = None
    reference_images: list[MediaContent] = field(default_factory=list)
    target_model: str | None = None
    routing_hint: str | None = None
    required_modality: str = "generative"
    template_name: str | None = None
    template_variables: dict[str, str] = field(default_factory=dict)
    character_id: str | None = None
    target_resolution: tuple[int, int] = (1920, 1080)
    target_fps: int = 8
    media_type: str = "image"
    duration_seconds: float = 5.0
    frame_count: int | None = None
    source_draft_id: str | None = None
    source_image_sha256: str | None = None
    keyframe_prompt: str | None = None
    motion_prompt: str | None = None
    prompt_language: str | None = None
    keyframe_language: str | None = None
    motion_language: str | None = None
    language_fallback_reason: str | None = None
    quality: str = "standard"
    preset_id: str | None = None
    required_vram_gb: float | None = None
    injection_method: str = "ip-adapter"
    api_key_id: str = ""
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    trace_id: str = ""


@dataclass
class VideoGenerationPlan:
    """视频请求的结构化提示词与时序计划."""

    source_prompt: str
    keyframe_prompt: str
    motion_prompt: str
    prompt_language: str
    keyframe_language: str
    motion_language: str
    duration_seconds: float
    fps: int
    frame_count: int
    source_draft_id: str | None = None
    source_image_sha256: str | None = None
    fallback_reason: str | None = None
    language_fallback_reason: str | None = None
    model_used: str | None = None
    cost_usd: float = 0.0

    def __post_init__(self) -> None:
        for field_name in (
            "source_prompt",
            "keyframe_prompt",
            "motion_prompt",
            "prompt_language",
            "keyframe_language",
            "motion_language",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if (
            isinstance(self.duration_seconds, bool)
            or not isinstance(self.duration_seconds, (int, float))
            or not math.isfinite(float(self.duration_seconds))
            or self.duration_seconds <= 0
        ):
            raise ValueError("duration_seconds must be a finite positive number")
        if isinstance(self.fps, bool) or not isinstance(self.fps, int) or self.fps <= 0:
            raise ValueError("fps must be a positive integer")
        if (
            isinstance(self.frame_count, bool)
            or not isinstance(self.frame_count, int)
            or self.frame_count <= 0
        ):
            raise ValueError("frame_count must be a positive integer")


@dataclass
class ComplexityEvaluation:
    """复杂度评估结果."""

    score: int
    factors: dict[str, Any] = field(default_factory=dict)
    recommended_model: str = ""


@dataclass
class RoutingDecision:
    """路由决策结果."""

    selected_model: str
    selected_provider: str
    reason: str = "complexity"
    complexity_score: int = 0
    estimated_cost: float = 0.0


@dataclass
class PromptOptimizationResult:
    """Prompt 优化结果."""

    optimized_prompt: str
    original_prompt: str
    template_used: str | None = None
    model_used: str | None = None
    cost_usd: float = 0.0
    duration_ms: float = 0.0
    source_language: str | None = None
    output_language: str | None = None
    language_fallback_reason: str | None = None


@dataclass
class CompressionResult:
    """Token 压缩结果."""

    feature_vector: list[float]
    original_token_count: int
    compressed_token_count: int
    compression_ratio: float
    duration_ms: float = 0.0


@dataclass
class DraftResult:
    """草图生成结果."""

    draft_id: str
    previews: list[bytes]
    generation_params: dict[str, Any]
    created_at: float
    expires_at: float
    attempt_number: int = 1
    max_attempts: int = 5
    status: str = "pending"
    media_type: str = "image"
    session_id: str | None = None
    user_id: str | None = None
    group_id: str | None = None
    video_id: str | None = None
    progress: float = 0.0
    stage: str = "queued"
    workflow_version: str = ""
    comfy_prompt_id: str | None = None
    worker_id: str | None = None
    device_uuid: str | None = None
    gpu_seconds: float = 0.0
    error: str | None = None


DRAFT_STATUS_GENERATING = "generating"
DRAFT_STATUS_QUEUED = "queued"
DRAFT_STATUS_RUNNING = "running"
DRAFT_STATUS_PENDING = "pending"
DRAFT_STATUS_CONFIRMING = "confirming"
DRAFT_STATUS_REFINING = "refining"
DRAFT_STATUS_CONFIRMED = "confirmed"
DRAFT_STATUS_COMPLETED = "completed"
DRAFT_STATUS_REJECTED = "rejected"
DRAFT_STATUS_EXPIRED = "expired"
DRAFT_STATUS_FAILED = "failed"
DRAFT_STATUS_CANCELLED = "cancelled"
DRAFT_VALID_STATUSES = (
    DRAFT_STATUS_GENERATING,
    DRAFT_STATUS_QUEUED,
    DRAFT_STATUS_RUNNING,
    DRAFT_STATUS_PENDING,
    DRAFT_STATUS_CONFIRMING,
    DRAFT_STATUS_REFINING,
    DRAFT_STATUS_CONFIRMED,
    DRAFT_STATUS_COMPLETED,
    DRAFT_STATUS_REJECTED,
    DRAFT_STATUS_EXPIRED,
    DRAFT_STATUS_FAILED,
    DRAFT_STATUS_CANCELLED,
)


@dataclass
class UpscaleResult:
    """高清放大结果."""

    draft_id: str
    output_data: bytes
    target_resolution: tuple[int, int]
    algorithm_used: str
    duration_ms: float = 0.0


@dataclass
class VideoSubmitResult:
    """视频任务提交结果."""

    draft_id: str
    video_id: str
    status: str = "generating"


@dataclass
class PromptTemplate:
    """提示词模板."""

    name: str
    content: str
    description: str = ""
    api_key_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def variables(self) -> list[str]:
        return re.findall(r"\{\{(\w+)\}\}", self.content)


@dataclass
class CostSavingRecord:
    """单次请求的成本节省记录."""

    request_id: str
    model_routing_saving_usd: float = 0.0
    token_compression_saving_usd: float = 0.0
    prompt_optimization_saving_usd: float = 0.0
    total_saving_usd: float = 0.0
    timestamp: float = 0.0
