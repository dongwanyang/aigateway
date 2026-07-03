"""
DraftGeneratorPlugin — 渐进式生成工作流插件封装
================================================

将 DraftGeneratorStrategy 封装为 PipelineEngine 插件，注册到 PluginRegistry。
在 execute() 中创建子 span，判断是否为生成请求并启用 Draft 工作流，
启用时生成低分辨率草图供用户预览确认。

需求: 3.6, 1.8
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from aigateway_core.context import PipelineContext
from aigateway_core.generation_optimization.config import GenerationOptimizationConfig
from aigateway_core.generation_optimization.models import (
    DraftResult,
    GenerationRequest,
)
from aigateway_core.generation_optimization.strategies.draft_generator import (
    DraftGeneratorStrategy,
)
from aigateway_core.tracing import TracingManager, get_tracing_manager

logger = logging.getLogger(__name__)

# 命名空间常量
NS_GENERATION_OPTIMIZATION = "generation_optimization"


class DraftGeneratorPlugin:
    """渐进式生成工作流插件 — 将 DraftGeneratorStrategy 封装为 PipelineEngine 插件.

    通过 PluginRegistry 注册后由 PipelineEngine 自动调度执行。
    依赖 token_compressor 插件先行执行。

    行为:
    - 禁用时: 透传请求，不做任何修改
    - 启用时:
      1. 检查请求是否为生成请求（需要 Draft 工作流）
      2. 从上下文构建 GenerationRequest
      3. 调用 strategy.generate_draft() 生成草图
      4. 将 draft_id 和 previews 写入 ctx.extra["generation_optimization"]["draft_generator"]
      5. 记录 span 属性

    Attributes:
        name: 插件名称 "draft_generator"
        enabled: 是否启用
        depends_on: 依赖的插件列表 ["token_compressor"]
    """

    name: str = "draft_generator"
    enabled: bool = True
    depends_on: List[str] = ["token_compressor"]

    def __init__(
        self,
        strategy: DraftGeneratorStrategy,
        config: GenerationOptimizationConfig,
    ) -> None:
        """初始化 DraftGeneratorPlugin.

        Args:
            strategy: DraftGeneratorStrategy 实例，负责 Draft 工作流核心逻辑
            config: 生成优化层主配置实例
        """
        self._strategy = strategy
        self._config = config

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        """执行 Draft 工作流逻辑.

        流程:
        1. 检查 Draft 工作流是否启用，禁用时直接透传
        2. 创建子 span 用于追踪 (需求 1.8)
        3. 检查请求是否为生成请求（需要 Draft 工作流）
        4. 如果 Draft 工作流启用且适用:
           - 从上下文构建 GenerationRequest
           - 调用 strategy.generate_draft()
           - 将 draft_id 和 previews 写入上下文
        5. 记录 span 属性

        Args:
            ctx: 管线上下文

        Returns:
            修改后的管线上下文
        """
        # 检查是否禁用 — 禁用时透传不做修改
        if not self._config.draft_workflow.enabled:
            logger.debug(
                "generation_optimization.draft_generator.disabled",
                extra={
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                },
            )
            return ctx

        start_time = time.monotonic()

        # 创建子 span (需求 1.8)
        tracing = get_tracing_manager()
        span_context = tracing.create_plugin_span(
            span_context={"trace_id": ctx.trace_id},
            plugin_name=self.name,
            request_id=ctx.request_id,
        )

        try:
            # 判断是否为生成请求（需要 Draft 工作流）
            if not self._is_generation_request(ctx):
                duration_ms = (time.monotonic() - start_time) * 1000.0
                gen_opt = ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})
                gen_opt["draft_generator"] = {
                    "applicable": False,
                    "reason": "not_a_generation_request",
                    "duration_ms": duration_ms,
                }
                logger.debug(
                    "generation_optimization.draft_generator.skipped",
                    extra={
                        "request_id": ctx.request_id,
                        "trace_id": ctx.trace_id,
                        "reason": "not_a_generation_request",
                    },
                )
                return ctx

            # 从上下文构建 GenerationRequest
            generation_request = self._build_generation_request(ctx)

            # 提取显式指定的关键帧数量（如果有）
            keyframe_count = self._extract_keyframe_count(ctx)

            # 调用 strategy.generate_draft()
            draft_result: DraftResult = await self._strategy.generate_draft(
                request=generation_request,
                config=self._config.draft_workflow,
                keyframe_count=keyframe_count,
            )

            # 计算耗时
            duration_ms = (time.monotonic() - start_time) * 1000.0

            # 将 draft_id 和 previews 写入 ctx.extra
            gen_opt = ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})
            gen_opt["draft_generator"] = {
                "applicable": True,
                "draft_id": draft_result.draft_id,
                "preview_count": len(draft_result.previews),
                "attempt_number": draft_result.attempt_number,
                "max_attempts": draft_result.max_attempts,
                "expires_at": draft_result.expires_at,
                "status": draft_result.status,
                "generation_params": draft_result.generation_params,
                "duration_ms": duration_ms,
            }

            # 记录 span 属性
            if span_context:
                attrs = span_context.get("attributes", {})
                attrs["draft_generator.draft_id"] = draft_result.draft_id
                attrs["draft_generator.preview_count"] = len(draft_result.previews)
                attrs["draft_generator.attempt_number"] = draft_result.attempt_number
                attrs["draft_generator.duration_ms"] = round(duration_ms, 2)

            logger.info(
                "generation_optimization.draft_generator.completed",
                extra={
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                    "draft_id": draft_result.draft_id,
                    "preview_count": len(draft_result.previews),
                    "attempt_number": draft_result.attempt_number,
                    "duration_ms": round(duration_ms, 2),
                },
            )

        except Exception as exc:
            duration_ms = (time.monotonic() - start_time) * 1000.0

            # 标记 span 为错误状态
            if span_context:
                TracingManager.mark_span_error(span_context.get("span"), exc)

            logger.warning(
                "generation_optimization.draft_generator.error",
                extra={
                    "reason": str(exc),
                    "fallback_action": "passthrough",
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                    "duration_ms": round(duration_ms, 2),
                },
            )
            # 故障降级: 写入错误信息，不阻断管线
            gen_opt = ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})
            gen_opt["draft_generator"] = {
                "applicable": True,
                "draft_id": None,
                "preview_count": 0,
                "duration_ms": duration_ms,
                "error": str(exc),
            }

        return ctx

    def _is_generation_request(self, ctx: PipelineContext) -> bool:
        """判断当前请求是否为需要 Draft 工作流的生成请求.

        判断条件（满足任一即视为生成请求）:
        1. 请求中显式标记了 generation_mode 或 draft_workflow
        2. Intent Evaluator 已在上下文中标记为 generative 类型
        3. 请求模型为 generative 类型

        Args:
            ctx: 管线上下文

        Returns:
            True 如果是生成请求且适用 Draft 工作流
        """
        # 检查请求中是否显式启用 draft workflow
        if ctx.request.get("draft_workflow") or ctx.request.get("enable_draft"):
            return True

        # 检查 Intent Evaluator 是否已标记 required_modality 为 generative
        gen_opt = ctx.extra.get(NS_GENERATION_OPTIMIZATION, {})
        intent_result = gen_opt.get("intent_evaluator", {})
        if intent_result.get("required_modality") == "generative":
            return True

        # 检查请求中的 generation_mode 标志
        if ctx.request.get("generation_mode"):
            return True

        # 检查模型名中是否包含生成类关键词
        model = ctx.request.get("model", "")
        if isinstance(model, str):
            gen_keywords = ["image", "video", "generative", "dall-e", "stable-diffusion"]
            model_lower = model.lower()
            if any(kw in model_lower for kw in gen_keywords):
                return True

        return False

    def _build_generation_request(self, ctx: PipelineContext) -> GenerationRequest:
        """从 PipelineContext 构建 GenerationRequest.

        提取上下文中的 prompt、参考图、目标分辨率等信息。

        Args:
            ctx: 管线上下文

        Returns:
            GenerationRequest 实例
        """
        # 提取 prompt
        prompt = self._extract_prompt(ctx)

        # 从请求中提取目标分辨率
        target_resolution = ctx.request.get("target_resolution", (1920, 1080))
        if isinstance(target_resolution, list):
            target_resolution = tuple(target_resolution)

        # 提取 target_fps
        target_fps = ctx.request.get("target_fps", 60)

        # 提取 api_key_id
        api_key_id = ctx.request.get("api_key_id", "")
        if not api_key_id and ctx.user_id:
            api_key_id = ctx.user_id

        return GenerationRequest(
            prompt=prompt,
            target_resolution=target_resolution,
            target_fps=target_fps,
            api_key_id=api_key_id or "",
            request_id=ctx.request_id,
        )

    def _extract_prompt(self, ctx: PipelineContext) -> str:
        """从请求中提取用户 prompt.

        优先使用 AI Director 优化后的 prompt（如果有）。

        Args:
            ctx: 管线上下文

        Returns:
            用户 prompt 字符串
        """
        # 优先使用 AI Director 优化后的 prompt
        gen_opt = ctx.extra.get(NS_GENERATION_OPTIMIZATION, {})
        ai_director_result = gen_opt.get("ai_director", {})
        optimized_prompt = ai_director_result.get("optimized_prompt")
        if optimized_prompt:
            return optimized_prompt

        # 从请求的 messages 中提取
        messages = ctx.request.get("messages", [])
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

        # 直接从请求的 prompt 字段获取
        return ctx.request.get("prompt", "")

    def _extract_keyframe_count(self, ctx: PipelineContext) -> Optional[int]:
        """从请求中提取显式指定的关键帧数量.

        Args:
            ctx: 管线上下文

        Returns:
            用户指定的关键帧数量，未指定时返回 None
        """
        keyframe_count = ctx.request.get("keyframe_count")
        if keyframe_count is not None:
            try:
                return int(keyframe_count)
            except (TypeError, ValueError):
                return None
        return None
