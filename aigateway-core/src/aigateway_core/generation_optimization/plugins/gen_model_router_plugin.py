"""
GenModelRouterPlugin — 智能模型路由插件封装
=============================================

将 ModelRouterStrategy 封装为 PipelineEngine 插件，注册到 PluginRegistry。
在 execute() 中通过 emit_plugin_event 发 TraceEvent,记录路由决策到请求元数据（模型、provider、原因、分数）。
ModelRoutingError 时返回错误响应，其他异常时回退到配置的 default_model 并记录日志。

需求: 2.7, 2.8, 1.8
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from aigateway_core.context import PipelineContext
from aigateway_core.generation_optimization.config import GenerationOptimizationConfig
from aigateway_core.generation_optimization.exceptions import ModelRoutingError
from aigateway_core.generation_optimization.models import RoutingDecision
from aigateway_core.generation_optimization.strategies.model_router import (
    ModelRouterStrategy,
)

logger = logging.getLogger(__name__)

# 命名空间常量
NS_GENERATION_OPTIMIZATION = "generation_optimization"


class GenModelRouterPlugin:
    """智能模型路由插件 — 将 ModelRouterStrategy 封装为 PipelineEngine 插件.

    通过 PluginRegistry 注册后由 PipelineEngine 自动调度执行。
    依赖 draft_generator 插件先行执行（确保 Draft 工作流逻辑已完成）。

    行为:
    - 禁用时: 透传请求，不做任何修改
    - 启用时: 从上下文中获取 complexity_score，调用 ModelRouterStrategy
      执行路由决策，将结果写入:
      1. ctx.extra["generation_optimization"]["model_router"] — 详细路由信息
      2. ctx.model_router["selected_model"] — 下游兼容字段
    - ModelRoutingError: 设置错误响应，标记 should_stop
    - 其他异常: 回退到 default_model，不阻断管线

    Attributes:
        name: 插件名称 "gen_model_router"
        enabled: 是否启用
        depends_on: 依赖的插件列表 ["draft_generator"]
    """

    name: str = "gen_model_router"
    enabled: bool = True
    depends_on: List[str] = ["draft_generator"]

    def __init__(
        self,
        strategy: ModelRouterStrategy,
        config: GenerationOptimizationConfig,
    ) -> None:
        """初始化 GenModelRouterPlugin.

        Args:
            strategy: ModelRouterStrategy 实例，负责路由决策核心逻辑
            config: 生成优化层主配置实例
        """
        self._strategy = strategy
        self._config = config

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        """执行模型路由决策.

        流程:
        1. 检查模型路由是否启用，禁用时直接透传
        2. 创建子 span 用于追踪
        3. 从 ctx.extra["generation_optimization"]["intent_evaluator"]["score"] 获取复杂度评分
        4. 从请求类型确定 required_modality
        5. 从请求中获取 routing_hint 和 model_override
        6. 调用 strategy.route() 执行路由决策
        7. 将 RoutingDecision 写入 ctx.extra["generation_optimization"]["model_router"]
        8. 同时写入 ctx.model_router["selected_model"] 供下游使用
        9. ModelRoutingError: 返回错误响应
        10. 其他异常: 回退到 default_model

        Args:
            ctx: 管线上下文

        Returns:
            修改后的管线上下文
        """
        # 检查是否禁用 — 禁用时透传不做修改
        if not self._config.model_router.enabled:
            logger.debug(
                "generation_optimization.gen_model_router.disabled",
                extra={
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                },
            )
            return ctx

        start_time = time.monotonic()

        try:
            # 获取复杂度评分
            complexity_score = self._get_complexity_score(ctx)

            # 确定所需模态
            required_modality = self._determine_modality(ctx)

            # 获取路由提示和模型覆盖
            routing_hint = self._get_routing_hint(ctx)
            model_override = self._get_model_override(ctx)

            # 调用策略执行路由决策
            decision: RoutingDecision = await self._strategy.route(
                complexity_score=complexity_score,
                required_modality=required_modality,
                routing_hint=routing_hint,
                model_override=model_override,
            )

            # 计算耗时
            duration_ms = (time.monotonic() - start_time) * 1000.0

            # 写入路由决策到 ctx.extra["generation_optimization"]["model_router"]
            self._write_routing_decision(ctx, decision, duration_ms)

            # 写入 ctx.model_router["selected_model"] 供下游兼容
            ctx.model_router["selected_model"] = decision.selected_model
            ctx.model_router["selected_provider"] = decision.selected_provider

            logger.info(
                "generation_optimization.gen_model_router.completed",
                extra={
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                    "selected_model": decision.selected_model,
                    "selected_provider": decision.selected_provider,
                    "reason": decision.reason,
                    "complexity_score": decision.complexity_score,
                    "estimated_cost": decision.estimated_cost,
                    "duration_ms": round(duration_ms, 2),
                },
            )

            # 发 TraceEvent(成功)
            from aigateway_core.generation_optimization.plugins import emit_plugin_event

            emit_plugin_event(ctx, self.name, duration_ms, "ok")

        except ModelRoutingError as exc:
            # ModelRoutingError: 返回错误响应，标记管线停止
            duration_ms = (time.monotonic() - start_time) * 1000.0

            # 发 TraceEvent(失败)
            from aigateway_core.generation_optimization.plugins import emit_plugin_event

            emit_plugin_event(ctx, self.name, duration_ms, "error")

            logger.error(
                "generation_optimization.gen_model_router.routing_error",
                extra={
                    "reason": str(exc),
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                    "duration_ms": round(duration_ms, 2),
                },
            )
            # 设置错误响应
            error_response = {
                "error": {
                    "message": str(exc),
                    "type": "model_routing_error",
                    "code": "model_not_available",
                }
            }
            ctx.response = json.dumps(error_response)
            ctx.mark_stopped(reason=f"ModelRoutingError: {exc}")

        except Exception as exc:
            # 其他异常: 回退到 default_model，不阻断管线
            duration_ms = (time.monotonic() - start_time) * 1000.0
            default_model = self._config.model_router.default_model

            # 发 TraceEvent(失败)
            from aigateway_core.generation_optimization.plugins import emit_plugin_event

            emit_plugin_event(ctx, self.name, duration_ms, "error")

            logger.warning(
                "generation_optimization.gen_model_router.fallback",
                extra={
                    "reason": str(exc),
                    "fallback_action": "use_default_model",
                    "default_model": default_model,
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                    "duration_ms": round(duration_ms, 2),
                },
            )

            # 获取复杂度分数（可能获取失败，用默认值）
            complexity_score = self._get_complexity_score_safe(ctx)

            # 构建回退决策
            fallback_decision = RoutingDecision(
                selected_model=default_model,
                selected_provider="unknown",
                reason="fallback",
                complexity_score=complexity_score,
                estimated_cost=0.0,
            )

            # 写入回退决策
            self._write_routing_decision(ctx, fallback_decision, duration_ms, error=str(exc))

            # 写入 ctx.model_router 供下游兼容
            ctx.model_router["selected_model"] = default_model
            ctx.model_router["selected_provider"] = "unknown"

        return ctx

    def _get_complexity_score(self, ctx: PipelineContext) -> int:
        """从上下文中获取复杂度评分.

        Args:
            ctx: 管线上下文

        Returns:
            复杂度评分 (0-100)

        Raises:
            KeyError: 如果 intent_evaluator 结果不存在
        """
        gen_opt = ctx.extra.get(NS_GENERATION_OPTIMIZATION, {})
        intent_result = gen_opt.get("intent_evaluator", {})
        score = intent_result.get("score")
        if score is None:
            raise KeyError(
                "intent_evaluator score not found in context; "
                "IntentEvaluatorPlugin may not have executed"
            )
        return int(score)

    def _get_complexity_score_safe(self, ctx: PipelineContext) -> int:
        """安全获取复杂度评分，失败时返回默认值 50.

        Args:
            ctx: 管线上下文

        Returns:
            复杂度评分 (0-100)
        """
        try:
            return self._get_complexity_score(ctx)
        except (KeyError, TypeError, ValueError):
            return 50

    def _determine_modality(self, ctx: PipelineContext) -> str:
        """从请求类型确定所需模态.

        判断逻辑:
        - 如果请求中有 required_modality 字段，直接使用
        - 如果是图片/视频/音频生成请求，使用 "generative"
        - 默认使用 "generative"

        Args:
            ctx: 管线上下文

        Returns:
            模态字符串: "llm" | "mllm" | "generative"
        """
        request = ctx.request

        # 优先从请求中获取明确的 modality 指定
        if "required_modality" in request:
            modality = request["required_modality"]
            if modality in ("llm", "mllm", "generative"):
                return modality

        # 从 generation_optimization 命名空间获取
        gen_opt = ctx.extra.get(NS_GENERATION_OPTIMIZATION, {})
        if "required_modality" in gen_opt:
            modality = gen_opt["required_modality"]
            if modality in ("llm", "mllm", "generative"):
                return modality

        # 默认为生成模型
        return "generative"

    def _get_routing_hint(self, ctx: PipelineContext) -> Optional[str]:
        """从请求中获取路由提示.

        Args:
            ctx: 管线上下文

        Returns:
            路由提示字符串或 None
        """
        request = ctx.request
        return request.get("routing_hint") or request.get("route_hint")

    def _get_model_override(self, ctx: PipelineContext) -> Optional[str]:
        """从请求中获取模型覆盖.

        Args:
            ctx: 管线上下文

        Returns:
            模型覆盖字符串或 None
        """
        request = ctx.request
        # OpenAI 格式使用 "model" 字段
        model = request.get("model")
        # 如果 model 字段存在且不是通配符/默认值，视为 model_override
        if model and model not in ("auto", "default", ""):
            # 检查是否是明确的覆盖意图（target_model 字段）
            target_model = request.get("target_model")
            if target_model:
                return target_model
            # 如果请求中有 model 字段且有 model_override 标记
            if request.get("model_override"):
                return request["model_override"]
        return request.get("target_model")

    def _write_routing_decision(
        self,
        ctx: PipelineContext,
        decision: RoutingDecision,
        duration_ms: float,
        error: Optional[str] = None,
    ) -> None:
        """将路由决策写入上下文.

        写入位置: ctx.extra["generation_optimization"]["model_router"]

        记录内容包括: 选中模型、provider、路由原因、复杂度评分、预估成本、耗时。

        Args:
            ctx: 管线上下文
            decision: 路由决策结果
            duration_ms: 路由耗时（毫秒）
            error: 错误信息（如果是回退场景）
        """
        gen_opt = ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})
        router_data: Dict[str, Any] = {
            "selected_model": decision.selected_model,
            "selected_provider": decision.selected_provider,
            "reason": decision.reason,
            "complexity_score": decision.complexity_score,
            "estimated_cost": decision.estimated_cost,
            "duration_ms": duration_ms,
        }
        if error:
            router_data["error"] = error
        gen_opt["model_router"] = router_data
