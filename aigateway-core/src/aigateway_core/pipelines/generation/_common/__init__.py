"""
generation_optimization — 生成优化层
=====================================

AI Gateway 平台的核心成本优化组件，位于用户生成请求与昂贵生成模型之间。
"""

from __future__ import annotations

__version__ = "0.1.0"

from aigateway_core.pipelines.generation._common.api_key_groups import (
    build_api_key_groups,
)
from aigateway_core.pipelines.generation._common.config import (
    GenerationOptimizationConfig,
    GenerationOptimizationConfigWatcher,
    parse_generation_optimization_config,
    validate_generation_optimization_config,
)
from aigateway_core.pipelines.generation._common.exceptions import (
    ConfigValidationError,
    DraftWorkflowError,
    FeatureCacheError,
    GenerationOptimizationError,
    ModelRoutingError,
    PromptOptimizationError,
    TemplateValidationError,
    TokenCompressionError,
)
from aigateway_core.pipelines.generation._common.metrics import (
    DEFAULT_API_KEY_GROUP,
    GenerationCostTracker,
    PrometheusMetricsRegistry,
    get_prometheus_registry,
    reset_prometheus_registry,
)
from aigateway_core.pipelines.generation._common.models import (
    ComplexityEvaluation,
    CompressionResult,
    CostSavingRecord,
    DraftResult,
    GenerationRequest,
    PromptOptimizationResult,
    PromptTemplate,
    RoutingDecision,
    UpscaleResult,
)

__all__ = [
    "DEFAULT_API_KEY_GROUP",
    "ComplexityEvaluation",
    "CompressionResult",
    "ConfigValidationError",
    "CostSavingRecord",
    "DraftResult",
    "DraftWorkflowError",
    "FeatureCacheError",
    "GenerationCostTracker",
    "GenerationOptimizationConfig",
    "GenerationOptimizationConfigWatcher",
    "GenerationOptimizationError",
    "GenerationRequest",
    "ModelRoutingError",
    "PrometheusMetricsRegistry",
    "PromptOptimizationError",
    "PromptOptimizationResult",
    "PromptTemplate",
    "RoutingDecision",
    "TemplateValidationError",
    "TokenCompressionError",
    "UpscaleResult",
    "__version__",
    "build_api_key_groups",
    "get_prometheus_registry",
    "parse_generation_optimization_config",
    "reset_prometheus_registry",
    "validate_generation_optimization_config",
]
