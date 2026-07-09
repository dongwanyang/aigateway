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

from aigateway_core.pipelines.generation.director.ai_director_plugin import (
    AIDirectorPlugin,
)
from aigateway_core.pipelines.generation.cost.cost_tracker_plugin import (
    CostTrackerPlugin,
)
from aigateway_core.pipelines.generation.draft.draft_generator_plugin import (
    DraftGeneratorPlugin,
)
from aigateway_core.pipelines.generation.routing_signals.gen_model_router_plugin import (
    GenModelRouterPlugin,
)
from aigateway_core.pipelines.generation.intent.intent_evaluator_plugin import (
    IntentEvaluatorPlugin,
)
from aigateway_core.pipelines.generation.token.token_compressor_plugin import (
    TokenCompressorPlugin,
)

logger = logging.getLogger(__name__)


def emit_plugin_event(
    ctx: Any,
    name: str,
    duration_ms: float,
    status: str = "ok",
    payload: Optional[dict] = None,
) -> None:
    """gen-opt 插件发 TraceEvent 的统一入口.

    取代旧的 tracing.create_plugin_span / mark_span_error 假 span 路径——
    6 个 gen-opt 插件在 execute() 成功/失败两路都调本函数,把一条 kind="plugin"
    的 TraceEvent 累积到当前请求的 TraceCollector(由 contextvar 隔离)。
    collector 不存在(单元测试未 start)时静默 no-op,不影响插件主逻辑。

    Args:
        ctx: PipelineContext,取 ctx.trace_id 关联到当前 collector
        name: 插件名(如 "ai_director"),作为 stage 与事件名前缀
        duration_ms: 本次 execute 耗时(毫秒)
        status: "ok" | "error" | "skip"
        payload: 可选附加字段(对应 debug 开关打开时填的内容)
    """
    import time as _time

    from aigateway_core.shared.trace_event import TraceCollector, TraceEvent

    collector = TraceCollector.current()
    if collector is None:
        return
    collector.emit(
        TraceEvent(
            trace_id=ctx.trace_id,
            ts=_time.monotonic(),
            stage=name,
            kind="plugin",
            name=f"{name}.execute",
            duration_ms=round(duration_ms, 2),
            status=status,
            payload=payload,
        )
    )


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
    from aigateway_core.pipelines.generation._common.config import (
        GenerationOptimizationConfig,
        parse_generation_optimization_config,
    )
    from aigateway_core.pipelines.generation._common.metrics import (
        GenerationCostTracker,
        get_prometheus_registry,
    )
    from aigateway_core.pipelines.generation._common.api_key_groups import (
        build_api_key_groups,
    )
    from aigateway_core.pipelines.generation.director.ai_director import (
        AIDirectorStrategy,
    )
    from aigateway_core.pipelines.generation.draft.draft_generator import (
        DraftGeneratorStrategy,
    )
    from aigateway_core.pipelines.generation.token.feature_cache import (
        FeatureCacheManager,
    )
    from aigateway_core.pipelines.generation.intent.intent_evaluator import (
        IntentEvaluatorStrategy,
    )
    from aigateway_core.route.model_resolution.model_router import (
        ModelRouterStrategy,
    )
    from aigateway_core.pipelines.generation.token.token_compressor import (
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
    from aigateway_core.shared.integration_configs import CLIPConfig, ComfyUIConfig
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
    # pipeline_kind="generation": 6 个生成优化插件归属生成管道，
    # 由生成管道的 PipelineEngine 单独装载，不混入理解管道。
    plugin_definitions = [
        {
            "name": "ai_director",
            "plugin_class": AIDirectorPlugin,
            "enabled": global_enabled and config.ai_director.enabled,
            # 生成管道不依赖理解管道的 prompt_cache，去掉跨管道依赖
            "depends_on": [],
            "priority": 100,
            "pipeline_kind": "generation",
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
            "pipeline_kind": "generation",
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
            "pipeline_kind": "generation",
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
            "pipeline_kind": "generation",
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
            "pipeline_kind": "generation",
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
            "pipeline_kind": "generation",
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
                pipeline_kind=plugin_def.get("pipeline_kind", "generation"),
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
    "emit_plugin_event",
    "register_generation_optimization_plugins",
]
