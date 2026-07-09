"""
IntentEvaluatorPlugin — 意图评估插件封装
==========================================

将 IntentEvaluatorStrategy 封装为 PipelineEngine 插件，注册到 PluginRegistry。
在 execute() 中通过 emit_plugin_event 发 TraceEvent,记录 complexity_score，禁用时透传请求不做修改。
评估失败时使用默认分数并记录日志。

需求: 2.7, 2.8, 1.8
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.pipelines.generation._common.config import GenerationOptimizationConfig
from aigateway_core.pipelines.generation._common.models import ComplexityEvaluation
from aigateway_core.pipelines.generation.intent.intent_evaluator import (
    IntentEvaluatorStrategy,
)

logger = logging.getLogger(__name__)

# 命名空间常量
NS_GENERATION_OPTIMIZATION = "generation_optimization"

# 默认复杂度评分（评估失败时使用，对应 mid-tier 路由）
DEFAULT_COMPLEXITY_SCORE = 50


class IntentEvaluatorPlugin:
    """意图评估插件 — 将 IntentEvaluatorStrategy 封装为 PipelineEngine 插件.

    通过 PluginRegistry 注册后由 PipelineEngine 自动调度执行。
    依赖 ai_director 插件先行执行（使用优化后的 prompt 进行评估）。

    行为:
    - 禁用时: 透传请求，不做任何修改
    - 启用时: 从上下文中提取 prompt 和生成参数，调用 IntentEvaluatorStrategy
      进行复杂度评估，将结果写入 ctx.extra["generation_optimization"]["intent_evaluator"]
    - 评估失败时: 使用默认分数 (50) 继续，不阻断管线

    Attributes:
        name: 插件名称 "intent_evaluator"
        enabled: 是否启用
        depends_on: 依赖的插件列表 ["ai_director"]
    """

    name: str = "intent_evaluator"
    enabled: bool = True
    depends_on: List[str] = ["ai_director"]

    def __init__(
        self,
        strategy: IntentEvaluatorStrategy,
        config: GenerationOptimizationConfig,
    ) -> None:
        """初始化 IntentEvaluatorPlugin.

        Args:
            strategy: IntentEvaluatorStrategy 实例，负责复杂度评估核心逻辑
            config: 生成优化层主配置实例
        """
        self._strategy = strategy
        self._config = config

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        """执行意图评估.

        流程:
        1. 检查模型路由是否启用，禁用时直接透传
        2. 创建子 span 用于追踪
        3. 从上下文中提取 prompt（优先使用 AI Director 优化后的 prompt）
        4. 从请求中提取 generation_params
        5. 调用 strategy.evaluate() 执行评估
        6. 将 ComplexityEvaluation 写入 ctx.extra["generation_optimization"]["intent_evaluator"]
        7. 记录 span 属性（complexity_score）
        8. 异常处理: 记录警告日志，设置默认分数

        Args:
            ctx: 管线上下文

        Returns:
            修改后的管线上下文
        """
        # 检查是否禁用 — 禁用时透传不做修改
        if not self._config.model_router.enabled:
            logger.debug(
                "generation_optimization.intent_evaluator.disabled",
                extra={
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                },
            )
            return ctx

        start_time = time.monotonic()

        try:
            # 从上下文中提取 prompt（优先使用 AI Director 优化后的 prompt）
            prompt = self._extract_prompt(ctx)

            # 从请求中提取生成参数
            generation_params = self._extract_generation_params(ctx)

            # 从上下文中提取参考图列表（用于评估）
            reference_images = self._extract_reference_images(ctx)

            # 调用策略执行评估
            evaluation: ComplexityEvaluation = self._strategy.evaluate(
                prompt=prompt,
                reference_images=reference_images,
                generation_params=generation_params,
            )

            # 计算耗时
            duration_ms = (time.monotonic() - start_time) * 1000.0

            # 写入评估结果到 ctx.extra
            gen_opt = ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})
            gen_opt["intent_evaluator"] = {
                "score": evaluation.score,
                "factors": evaluation.factors,
                "recommended_model": evaluation.recommended_model,
                "duration_ms": duration_ms,
            }

            logger.info(
                "generation_optimization.intent_evaluator.completed",
                extra={
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                    "complexity_score": evaluation.score,
                    "factors": evaluation.factors,
                    "duration_ms": round(duration_ms, 2),
                },
            )

            # 发 TraceEvent(成功)
            from aigateway_core.pipelines.generation.registration import emit_plugin_event

            emit_plugin_event(ctx, self.name, duration_ms, "ok")

        except Exception as exc:
            duration_ms = (time.monotonic() - start_time) * 1000.0

            # 发 TraceEvent(失败)
            from aigateway_core.pipelines.generation.registration import emit_plugin_event

            emit_plugin_event(ctx, self.name, duration_ms, "error")

            logger.warning(
                "generation_optimization.intent_evaluator.error",
                extra={
                    "reason": str(exc),
                    "fallback_action": "use_default_score",
                    "default_score": DEFAULT_COMPLEXITY_SCORE,
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                    "duration_ms": round(duration_ms, 2),
                },
            )
            # 故障降级: 使用默认分数，不阻断管线
            gen_opt = ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})
            gen_opt["intent_evaluator"] = {
                "score": DEFAULT_COMPLEXITY_SCORE,
                "factors": {},
                "recommended_model": "",
                "duration_ms": duration_ms,
                "error": str(exc),
            }

        return ctx

    def _extract_prompt(self, ctx: PipelineContext) -> str:
        """从上下文中提取 prompt，优先使用 AI Director 优化后的结果.

        查找顺序:
        1. ctx.extra["generation_optimization"]["ai_director"]["optimized_prompt"]
        2. 最后一条 user message 的 content

        Args:
            ctx: 管线上下文

        Returns:
            用于评估的 prompt 字符串
        """
        # 优先使用 AI Director 优化后的 prompt
        gen_opt = ctx.extra.get(NS_GENERATION_OPTIMIZATION, {})
        ai_director_result = gen_opt.get("ai_director", {})
        optimized_prompt = ai_director_result.get("optimized_prompt", "")
        if optimized_prompt:
            return optimized_prompt

        # 回退到原始请求中的 prompt
        messages = ctx.request.get("messages", [])
        if not messages:
            return ""

        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    text_parts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                    return " ".join(text_parts)
                return ""

        return ""

    def _extract_generation_params(self, ctx: PipelineContext) -> Dict[str, Any]:
        """从请求中提取生成参数.

        提取用于复杂度评估的生成参数，如目标分辨率、宽高等。

        Args:
            ctx: 管线上下文

        Returns:
            生成参数字典
        """
        params: Dict[str, Any] = {}

        # 从请求中提取常用生成参数
        request = ctx.request
        if "target_resolution" in request:
            params["target_resolution"] = request["target_resolution"]
        if "width" in request:
            params["width"] = request["width"]
        if "height" in request:
            params["height"] = request["height"]
        if "size" in request:
            # OpenAI 格式: "1024x1024"
            size = request["size"]
            if isinstance(size, str) and "x" in size:
                try:
                    w, h = size.split("x")
                    params["width"] = int(w)
                    params["height"] = int(h)
                except (ValueError, TypeError):
                    pass

        # 也从 generation_optimization 命名空间的元数据中获取
        gen_opt = ctx.extra.get(NS_GENERATION_OPTIMIZATION, {})
        if "generation_params" in gen_opt:
            params.update(gen_opt["generation_params"])

        return params

    def _extract_reference_images(self, ctx: PipelineContext) -> list:
        """从上下文中提取参考图列表.

        Args:
            ctx: 管线上下文

        Returns:
            参考图列表（用于评估，当前策略不需要图片内容，返回空列表即可）
        """
        # IntentEvaluatorStrategy.evaluate 接受 reference_images 参数
        # 但当前评估逻辑主要基于 prompt 分析，参考图暂不影响评分
        # 如果后续需要可以从 media_optimization 命名空间提取
        return []
