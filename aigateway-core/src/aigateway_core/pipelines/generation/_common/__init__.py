"""
generation_optimization — 生成优化层
=====================================

AI Gateway 平台的核心成本优化组件，位于用户生成请求与昂贵生成模型之间。
通过以下核心策略在保证输出质量的前提下大幅降低生成式 AI 的调用成本：

- AI 导演 Prompt 优化
- 智能模型路由
- 渐进式生成工作流（Draft-to-HiRes）
- 输入端视觉 Token 压缩与资产复用
- 成本追踪与 Prometheus 指标上报
- 提示词模板管理

该层以插件形式集成到现有 PipelineEngine，复用 ConfigManager、
MediaCacheManager、MetricsCollector 等基础设施。
"""

from __future__ import annotations

from functools import wraps

__version__ = "0.1.0"

from aigateway_core.pipelines.generation._common.api_key_groups import (
    build_api_key_groups,
)
from aigateway_core.pipelines.generation._common import config as _config_module
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

# ``DraftWorkflowConfig`` historically embedded ``/app/data/drafts`` in its
# generated dataclass constructor. Make an omitted value neutral so the runtime
# must receive ``generation_optimization.draft_workflow.store_dir`` from YAML.
_original_draft_config_init = _config_module.DraftWorkflowConfig.__init__


@wraps(_original_draft_config_init)
def _configured_draft_config_init(self, *args, **kwargs):
    # store_dir is the 14th dataclass field. Positional construction is retained
    # for compatibility, while normal keyword/config parsing receives an empty
    # value when the field is omitted.
    if "store_dir" not in kwargs and len(args) < 14:
        kwargs["store_dir"] = ""
    _original_draft_config_init(self, *args, **kwargs)


_config_module.DraftWorkflowConfig.__init__ = _configured_draft_config_init

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
