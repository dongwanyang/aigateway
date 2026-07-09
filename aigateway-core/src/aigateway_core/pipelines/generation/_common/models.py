"""
数据模型 — 生成优化层核心数据结构
===================================

定义 Generation Optimization Layer 使用的核心请求/响应数据结构，
包括生成请求、评估结果、路由决策、压缩结果、草图结果、模板和成本记录。

需求: 1.1, 2.1, 2.7, 3.1, 4.3, 5.1, 7.1, 8.2
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from aigateway_core.prefix.media.types import MediaContent


@dataclass
class GenerationRequest:
    """生成请求 — 包含优化所需的全部信息.

    封装用户提交的生成请求及其相关参数，是生成优化管线各阶段的
    统一输入数据结构。

    Attributes:
        prompt: 用户提示词
        reference_images: 参考图列表（MediaContent 对象）
        target_model: 模型覆盖，指定时绕过路由直接使用该模型
        routing_hint: 路由提示，如 "best quality"、"cheapest" 或具体模型名
        required_modality: 所需模态类别，决定路由筛选的模型类型
            "llm" — 纯文本语言模型
            "mllm" — 多模态理解模型
            "generative" — 生成模型（默认）
        template_name: 提示词模板名称，指定时跳过 AI Director 改写
        template_variables: 模板占位符变量值映射
        character_id: 角色 ID，用于特征缓存查找和复用
        target_resolution: 目标分辨率 (width, height)
        target_fps: 目标帧率（视频生成时使用）
        injection_method: 特征注入方式 ("ip-adapter" | "controlnet")
        api_key_id: API Key 标识符，用于资源隔离
        request_id: 请求唯一标识，默认自动生成 uuid4
    """

    prompt: str
    reference_images: List[MediaContent] = field(default_factory=list)
    target_model: Optional[str] = None
    routing_hint: Optional[str] = None
    required_modality: str = "generative"
    template_name: Optional[str] = None
    template_variables: Dict[str, str] = field(default_factory=dict)
    character_id: Optional[str] = None
    target_resolution: Tuple[int, int] = (1920, 1080)
    target_fps: int = 60
    media_type: str = "image"  # "image" | "video"
    injection_method: str = "ip-adapter"
    api_key_id: str = ""
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class ComplexityEvaluation:
    """复杂度评估结果.

    意图评估器分析生成请求后输出的评估结果，包含总分和各维度明细。

    Attributes:
        score: 复杂度评分，范围 0-100
        factors: 各评估维度评分明细，如 subject_count、interaction_type 等
        recommended_model: 基于评分推荐的模型标识符
    """

    score: int
    factors: Dict[str, Any] = field(default_factory=dict)
    recommended_model: str = ""


@dataclass
class RoutingDecision:
    """路由决策结果.

    模型路由器选择目标模型后输出的决策记录，包含选中模型信息和决策原因。

    Attributes:
        selected_model: 选中的模型标识符
        selected_provider: 选中的 provider 名称
        reason: 路由原因
            "complexity" — 基于复杂度评分选择
            "hint" — 基于用户路由提示选择
            "override" — 用户直接指定模型
            "fallback" — 降级到备选模型
        complexity_score: 本次路由使用的复杂度评分
        estimated_cost: 预估单次调用成本 (USD)
    """

    selected_model: str
    selected_provider: str
    reason: str = "complexity"
    complexity_score: int = 0
    estimated_cost: float = 0.0


@dataclass
class PromptOptimizationResult:
    """Prompt 优化结果.

    AI Director 对用户 prompt 进行改写或模板应用后的输出结果。

    Attributes:
        optimized_prompt: 优化后的 prompt
        original_prompt: 原始用户 prompt
        template_used: 使用的模板名称（如果是模板应用）
        model_used: 改写使用的模型名称（如果是模型改写）
        cost_usd: 改写模型调用成本 (USD)
        duration_ms: 优化耗时（毫秒）
    """

    optimized_prompt: str
    original_prompt: str
    template_used: Optional[str] = None
    model_used: Optional[str] = None
    cost_usd: float = 0.0
    duration_ms: float = 0.0


@dataclass
class CompressionResult:
    """Token 压缩结果.

    视觉 Token 压缩器对参考图进行语义级压缩后的输出结果。

    Attributes:
        feature_vector: 提取的特征向量
        original_token_count: 原始 Token 估算数（= file_size_bytes / 4）
        compressed_token_count: 压缩后 Token 数（= Feature Vector 维度数）
        compression_ratio: 实际压缩比
        duration_ms: 压缩耗时（毫秒）
    """

    feature_vector: List[float]
    original_token_count: int
    compressed_token_count: int
    compression_ratio: float
    duration_ms: float = 0.0


@dataclass
class DraftResult:
    """草图生成结果.

    Draft-to-HiRes 工作流中生成的低分辨率预览结果。

    Attributes:
        draft_id: 唯一草图标识
        previews: 预览图/关键帧列表（bytes 数据）
        generation_params: 生成参数快照
        created_at: 创建时间戳（Unix 秒）
        expires_at: 过期时间戳（Unix 秒）
        attempt_number: 当前重试次数
        max_attempts: 最大允许重试次数
        status: 草图状态
            "pending" — 等待用户确认
            "confirmed" — 已确认，可执行放大
            "rejected" — 已拒绝，可重新生成
            "expired" — 已过期，资源已释放
    """

    draft_id: str
    previews: List[bytes]
    generation_params: Dict[str, Any]
    created_at: float
    expires_at: float
    attempt_number: int = 1
    max_attempts: int = 5
    status: str = "pending"


# DraftResult 合法状态值
DRAFT_STATUS_PENDING = "pending"
DRAFT_STATUS_CONFIRMED = "confirmed"
DRAFT_STATUS_REJECTED = "rejected"
DRAFT_STATUS_EXPIRED = "expired"
DRAFT_VALID_STATUSES = (
    DRAFT_STATUS_PENDING,
    DRAFT_STATUS_CONFIRMED,
    DRAFT_STATUS_REJECTED,
    DRAFT_STATUS_EXPIRED,
)


@dataclass
class UpscaleResult:
    """高清放大结果.

    用户确认草图后执行超分辨率放大的输出结果。

    Attributes:
        draft_id: 关联的草图标识
        output_data: 放大后的图像/视频数据
        target_resolution: 目标分辨率 (width, height)
        algorithm_used: 使用的放大算法名称
        duration_ms: 放大耗时（毫秒）
    """

    draft_id: str
    output_data: bytes
    target_resolution: Tuple[int, int]
    algorithm_used: str
    duration_ms: float = 0.0


@dataclass
class PromptTemplate:
    """提示词模板.

    用户预先配置的结构化提示词模板，支持 {{variable_name}} 占位符语法。

    Attributes:
        name: 模板名称（1-64 字符，允许字母数字、连字符、下划线）
        content: 模板内容（最多 10000 字符）
        description: 模板描述（最多 500 字符）
        api_key_id: 所属 API Key 标识符
        created_at: 创建时间戳（Unix 秒）
        updated_at: 最后更新时间戳（Unix 秒）
    """

    name: str
    content: str
    description: str = ""
    api_key_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def variables(self) -> List[str]:
        """提取模板中的占位符变量名.

        扫描 content 中所有 {{variable_name}} 格式的占位符，
        返回去重后的变量名列表。

        Returns:
            变量名列表，例如 ["subject", "style", "background"]
        """
        return re.findall(r"\{\{(\w+)\}\}", self.content)


@dataclass
class CostSavingRecord:
    """单次请求的成本节省记录.

    记录一次生成请求经过优化管线后各策略带来的成本节省明细。

    Attributes:
        request_id: 关联的请求标识
        model_routing_saving_usd: 模型路由节省 (premium_price - actual_price)
        token_compression_saving_usd: Token 压缩节省
        prompt_optimization_saving_usd: Prompt 优化净节省（减少重试 - Director 成本）
        total_saving_usd: 总节省 = 各策略节省之和
        timestamp: 记录时间戳（Unix 秒）
    """

    request_id: str
    model_routing_saving_usd: float = 0.0
    token_compression_saving_usd: float = 0.0
    prompt_optimization_saving_usd: float = 0.0
    total_saving_usd: float = 0.0
    timestamp: float = 0.0
