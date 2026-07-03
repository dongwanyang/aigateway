"""
plugins — PipelineEngine 插件封装
================================

将各优化策略封装为 PipelineEngine 插件，通过 PluginRegistry 注册。
各插件通过 depends_on 声明依赖关系，由 PipelineEngine 拓扑排序执行。

插件列表：
- AIDirectorPlugin (depends_on: prompt_cache)
- IntentEvaluatorPlugin (depends_on: ai_director)
- TokenCompressorPlugin (depends_on: intent_evaluator)
- DraftGeneratorPlugin (depends_on: token_compressor)
- GenModelRouterPlugin (depends_on: draft_generator)
- CostTrackerPlugin (depends_on: gen_model_router)

使用 register_generation_optimization_plugins() 函数将所有插件
注册到 PluginRegistry，根据配置启用/禁用各插件。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from aigateway_core.generation_optimization.plugins.ai_director_plugin import (
    AIDirectorPlugin,
)
from aigateway_core.generation_optimization.plugins.cost_tracker_plugin import (
    CostTrackerPlugin,
)
from aigateway_core.generation_optimization.plugins.draft_generator_plugin import (
    DraftGeneratorPlugin,
)
from aigateway_core.generation_optimization.plugins.gen_model_router_plugin import (
    GenModelRouterPlugin,
)
from aigateway_core.generation_optimization.plugins.intent_evaluator_plugin import (
    IntentEvaluatorPlugin,
)
from aigateway_core.generation_optimization.plugins.token_compressor_plugin import (
    TokenCompressorPlugin,
)

logger = logging.getLogger(__name__)


def register_generation_optimization_plugins(
    registry: Any,
    config_manager: Any = None,
    redis_client: Any = None,
) -> None:
    """将 6 个生成优化插件注册到 PluginRegistry.

    根据 GenerationOptimizationConfig 中各策略的 enabled 开关决定是否启用。
    禁用的插件仍然注册（保持依赖链完整），但 enabled=False 时 execute() 直接透传。

    依赖链:
        ai_director → intent_evaluator → token_compressor →
        draft_generator → gen_model_router → cost_tracker

    Args:
        registry: PluginRegistry 实例
        config_manager: ConfigManager 实例，用于读取 generation_optimization 配置。
            若为 None，使用默认配置。
        redis_client: Redis 客户端实例（可选），用于 Feature Cache 和 Draft 存储。

    需求: 6.1, 6.6
    """
    from aigateway_core.generation_optimization.config import (
        GenerationOptimizationConfig,
        parse_generation_optimization_config,
    )
    from aigateway_core.generation_optimization.metrics import (
        GenerationCostTracker,
        get_prometheus_registry,
    )
    from aigateway_core.generation_optimization.api_key_groups import (
        build_api_key_groups,
    )
    from aigateway_core.generation_optimization.strategies.ai_director import (
        AIDirectorStrategy,
    )
    from aigateway_core.generation_optimization.strategies.draft_generator import (
        DraftGeneratorStrategy,
    )
    from aigateway_core.generation_optimization.strategies.feature_cache import (
        FeatureCacheManager,
    )
    from aigateway_core.generation_optimization.strategies.intent_evaluator import (
        IntentEvaluatorStrategy,
    )
    from aigateway_core.generation_optimization.strategies.model_router import (
        ModelRouterStrategy,
    )
    from aigateway_core.generation_optimization.strategies.token_compressor import (
        TokenCompressorStrategy,
    )

    # --- 加载配置 ---
    gen_opt_dict: Dict[str, Any] = {}
    providers_config: Dict[str, Any] = {}
    auth_config: Dict[str, Any] = {}

    if config_manager is not None:
        gen_opt_dict = config_manager.get("generation_optimization", {}) or {}
        providers_config = config_manager.get("providers", {}) or {}
        auth_config = config_manager.get("auth", {}) or {}

    config = parse_generation_optimization_config(gen_opt_dict)

    # 如果整个优化层被禁用，注册所有插件但全部设为 disabled
    global_enabled = config.enabled

    # --- 构建 API Key group 映射 ---
    api_key_groups = build_api_key_groups(auth_config)

    # --- 创建策略实例 ---
    ai_director_strategy = AIDirectorStrategy(config=config.ai_director)
    intent_evaluator_strategy = IntentEvaluatorStrategy(config=config.model_router)

    # 加载 CLIP 配置（从 generation_optimization.token_compressor.clip）
    from aigateway_core.integration_configs import CLIPConfig, ComfyUIConfig
    clip_dict = gen_opt_dict.get("token_compressor", {}).get("clip", {})
    clip_config = CLIPConfig(
        model_name=clip_dict.get("model_name", CLIPConfig.model_name),
        device=clip_dict.get("device", CLIPConfig.device),
        batch_size=clip_dict.get("batch_size", CLIPConfig.batch_size),
    ) if clip_dict else CLIPConfig()

    token_compressor_strategy = TokenCompressorStrategy(
        config=config.token_compressor,
        clip_config=clip_config,
    )

    # 加载 ComfyUI 配置（从 generation_optimization.draft_workflow.comfyui）
    comfyui_dict = gen_opt_dict.get("draft_workflow", {}).get("comfyui", {})
    comfyui_config = ComfyUIConfig(
        server_url=comfyui_dict.get("server_url", ComfyUIConfig.server_url),
        connect_timeout=comfyui_dict.get("connect_timeout", ComfyUIConfig.connect_timeout),
        execution_timeout=comfyui_dict.get("execution_timeout", ComfyUIConfig.execution_timeout),
    ) if comfyui_dict else ComfyUIConfig()

    draft_generator_strategy = DraftGeneratorStrategy(
        config=config.draft_workflow,
        redis_client=redis_client,
        comfyui_config=comfyui_config,
    )
    model_router_strategy = ModelRouterStrategy(
        config=config.model_router,
        providers_config=providers_config,
    )

    # Feature Cache Manager
    feature_cache = FeatureCacheManager(
        redis_client=redis_client,
        config=config.feature_cache,
    )

    # Cost Tracker
    cost_tracker = GenerationCostTracker(
        config=config.cost_tracking,
        prometheus_registry=get_prometheus_registry(),
        api_key_groups=api_key_groups,
    )

    # --- 定义插件注册列表（含依赖链和优先级） ---
    # priority 确保即使不依赖拓扑排序也能按正确顺序执行
    plugin_definitions = [
        {
            "name": "ai_director",
            "plugin_class": AIDirectorPlugin,
            "enabled": global_enabled and config.ai_director.enabled,
            "depends_on": ["prompt_cache"],
            "priority": 100,
            "config": {
                "strategy": ai_director_strategy,
                "config": config,
            },
        },
        {
            "name": "intent_evaluator",
            "plugin_class": IntentEvaluatorPlugin,
            "enabled": global_enabled and config.model_router.enabled,
            "depends_on": ["ai_director"],
            "priority": 110,
            "config": {
                "strategy": intent_evaluator_strategy,
                "config": config,
            },
        },
        {
            "name": "token_compressor",
            "plugin_class": TokenCompressorPlugin,
            "enabled": global_enabled and config.token_compressor.enabled,
            "depends_on": ["intent_evaluator"],
            "priority": 120,
            "config": {
                "strategy": token_compressor_strategy,
                "cache": feature_cache,
                "config": config,
            },
        },
        {
            "name": "draft_generator",
            "plugin_class": DraftGeneratorPlugin,
            "enabled": global_enabled and config.draft_workflow.enabled,
            "depends_on": ["token_compressor"],
            "priority": 130,
            "config": {
                "strategy": draft_generator_strategy,
                "config": config,
            },
        },
        {
            "name": "gen_model_router",
            "plugin_class": GenModelRouterPlugin,
            "enabled": global_enabled and config.model_router.enabled,
            "depends_on": ["draft_generator"],
            "priority": 140,
            "config": {
                "strategy": model_router_strategy,
                "config": config,
            },
        },
        {
            "name": "cost_tracker",
            "plugin_class": CostTrackerPlugin,
            "enabled": global_enabled and config.cost_tracking.enabled,
            "depends_on": ["gen_model_router"],
            "priority": 150,
            "config": {
                "tracker": cost_tracker,
                "config": config,
            },
        },
    ]

    # --- 注册插件 ---
    registered_count = 0
    for plugin_def in plugin_definitions:
        name = plugin_def["name"]
        try:
            registry.register(
                name=name,
                plugin_class=plugin_def["plugin_class"],
                enabled=plugin_def["enabled"],
                depends_on=plugin_def["depends_on"],
                priority=plugin_def["priority"],
                config=plugin_def["config"],
            )
            registered_count += 1
            logger.debug(
                "generation_optimization.plugin_registered: name=%s, enabled=%s",
                name,
                plugin_def["enabled"],
            )
        except ValueError:
            # 插件名已存在（可能已通过其他途径注册），跳过
            logger.debug(
                "generation_optimization.plugin_already_registered: name=%s",
                name,
            )

    logger.info(
        "generation_optimization.plugins_registered: count=%d, global_enabled=%s",
        registered_count,
        global_enabled,
    )


__all__ = [
    "AIDirectorPlugin",
    "CostTrackerPlugin",
    "DraftGeneratorPlugin",
    "GenModelRouterPlugin",
    "IntentEvaluatorPlugin",
    "TokenCompressorPlugin",
    "register_generation_optimization_plugins",
]
