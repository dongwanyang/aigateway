"""
strategies — 优化策略实现
========================

各生成优化策略的核心逻辑模块，包括：
- AIDirectorStrategy: AI 导演 Prompt 改写
- PromptConfirmationHandler: Prompt 确认流程
- IntentEvaluatorStrategy: 意图评估与复杂度打分
- ModelRouterStrategy: 智能模型路由
- TokenCompressorStrategy: 视觉 Token 压缩
- FeatureCacheManager: 特征向量缓存管理
- DraftGeneratorStrategy: 渐进式生成工作流
- PromptTemplateManager: 提示词模板 CRUD 管理
"""

from __future__ import annotations

from aigateway_core.generation_optimization.strategies.ai_director import (
    AIDirectorStrategy,
)
from aigateway_core.generation_optimization.strategies.feature_cache import (
    FeatureCacheManager,
)
from aigateway_core.generation_optimization.strategies.model_router import (
    ModelConfig,
    ModelRouterStrategy,
)
from aigateway_core.generation_optimization.strategies.prompt_confirmation import (
    PromptConfirmationHandler,
)
from aigateway_core.generation_optimization.strategies.draft_generator import (
    DraftGeneratorStrategy,
)
from aigateway_core.generation_optimization.strategies.prompt_template_manager import (
    PromptTemplateManager,
)
from aigateway_core.generation_optimization.strategies.token_compressor import (
    TokenCompressorStrategy,
)
from aigateway_core.generation_optimization.strategies.video_preview import (
    VideoPreviewGenerator,
)

__all__ = [
    "AIDirectorStrategy",
    "DraftGeneratorStrategy",
    "FeatureCacheManager",
    "ModelConfig",
    "ModelRouterStrategy",
    "PromptConfirmationHandler",
    "PromptTemplateManager",
    "TokenCompressorStrategy",
    "VideoPreviewGenerator",
]
