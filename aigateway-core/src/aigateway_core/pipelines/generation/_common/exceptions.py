"""
异常定义 — 生成优化层异常层次
============================

继承树:
    GenerationOptimizationError
    ├── PromptOptimizationError
    ├── ModelRoutingError
    ├── TokenCompressionError
    ├── DraftWorkflowError
    ├── FeatureCacheError
    ├── TemplateValidationError
    └── ConfigValidationError

需求: 6.1, 6.2, 6.7
"""

from __future__ import annotations


class GenerationOptimizationError(Exception):
    """生成优化层基础异常."""


class PromptOptimizationError(GenerationOptimizationError):
    """Prompt 优化失败（AI Director 超时/模型错误）."""


class ModelRoutingError(GenerationOptimizationError):
    """模型路由失败（所有候选模型不可用）."""


class TokenCompressionError(GenerationOptimizationError):
    """Token 压缩失败（超时/格式错误）."""


class DraftWorkflowError(GenerationOptimizationError):
    """Draft 工作流错误（重试耗尽/草图过期）."""


class FeatureCacheError(GenerationOptimizationError):
    """特征缓存错误（Redis 不可用 + 原图不可用）."""


class TemplateValidationError(GenerationOptimizationError):
    """模板验证错误（名称重复/变量缺失/权限不足）."""


class ConfigValidationError(GenerationOptimizationError):
    """配置验证错误（值无效/类型错误）."""
