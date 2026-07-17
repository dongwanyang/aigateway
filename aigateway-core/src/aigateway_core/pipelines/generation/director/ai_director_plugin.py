"""
AIDirectorPlugin — AI 导演插件封装
===================================

将 AIDirectorStrategy 封装为 PipelineEngine 插件，注册到 PluginRegistry。
在 execute() 中通过 PipelineEngine 自动埋点(成功/失败两路),禁用时透传请求不做修改。
根据是否有参考图选择模态: 有参考图用 mllm 模型，无参考图用 llm 模型。

需求: 1.7, 1.8, 2.10
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.pipelines.generation._common.config import GenerationOptimizationConfig
from aigateway_core.pipelines.generation.director.ai_director import (
    AIDirectorStrategy,
)
from aigateway_core.prefix.media.types import MediaContent, MediaType

logger = logging.getLogger(__name__)

# 命名空间常量
NS_GENERATION_OPTIMIZATION = "generation_optimization"


class AIDirectorPlugin:
    """AI 导演插件 — 将 AIDirectorStrategy 封装为 PipelineEngine 插件.

    通过 PluginRegistry 注册后由 PipelineEngine 自动调度执行。
    依赖 prompt_cache 插件先行执行（确保缓存相关逻辑已完成）。

    行为:
    - 禁用时: 透传请求，不做任何修改
    - 启用时: 从请求中提取 prompt 和参考图，调用 AIDirectorStrategy
      进行 prompt 优化，将结果写入 ctx.extra["generation_optimization"]["ai_director"]
    - 根据是否有参考图选择模态:
      - 有参考图 → mllm 模型（多模态理解）
      - 无参考图 → llm 模型（纯文本改写）

    Attributes:
        name: 插件名称 "ai_director"
        enabled: 是否启用
        depends_on: 依赖的插件列表 ["prompt_cache"]
    """

    name: str = "ai_director"
    enabled: bool = True
    depends_on: List[str] = ["prompt_cache"]

    def __init__(
        self,
        strategy: AIDirectorStrategy,
        config: GenerationOptimizationConfig,
    ) -> None:
        """初始化 AIDirectorPlugin.

        Args:
            strategy: AI Director 策略实例，负责 prompt 优化核心逻辑
            config: 生成优化层主配置实例
        """
        self._strategy = strategy
        self._config = config

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        """执行 AI 导演优化.

        流程:
        1. 检查 AI Director 是否启用，禁用时直接透传
        2. 从请求中提取用户 prompt 和参考图
        3. 根据是否有参考图确定模态（mllm/llm）
        4. 调用 strategy.optimize_prompt() 执行优化
        5. 将结果写入 ctx.extra["generation_optimization"]["ai_director"]
        6. PipelineEngine 自动埋点 TraceEvent(成功/失败两路)

        Args:
            ctx: 管线上下文

        Returns:
            修改后的管线上下文
        """
        # 检查是否禁用 — 禁用时透传不做修改
        if not self._config.ai_director.enabled:
            logger.debug(
                "generation_optimization.ai_director.disabled",
                extra={
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                },
            )
            return ctx

        start_time = time.monotonic()
        duration_ms = 0.0
        try:
            # 从请求中提取 prompt（最后一条 user message 的 content）
            prompt = self._extract_prompt(ctx)

            # 从上下文中提取参考图
            reference_images = self._extract_reference_images(ctx)

            # 根据是否有参考图选择模态
            # 有参考图 → mllm 模型，无参考图 → llm 模型
            modality = "mllm" if reference_images else "llm"

            # 调用策略执行 prompt 优化
            result = await self._strategy.optimize_prompt(
                prompt=prompt,
                reference_images=reference_images,
                config=self._config.ai_director,
                ctx=ctx,
            )

            # 计算耗时
            duration_ms = (time.monotonic() - start_time) * 1000.0

            # 写入优化结果到 ctx.extra
            gen_opt = ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})
            gen_opt["ai_director"] = {
                "optimized_prompt": result.optimized_prompt,
                "original_prompt": result.original_prompt,
                "template_used": result.template_used,
                "model_used": result.model_used,
                "modality": modality,
                "cost_usd": result.cost_usd,
                "duration_ms": duration_ms,
                "has_reference_images": bool(reference_images),
                "reference_image_count": len(reference_images),
            }

            logger.info(
                "generation_optimization.ai_director.completed",
                extra={
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                    "modality": modality,
                    "model_used": result.model_used,
                    "prompt_length": len(result.optimized_prompt),
                    "duration_ms": round(duration_ms, 2),
                },
            )

            # 记录插件 trace（业务 metadata）
            ctx.add_plugin_trace(
                "ai_director", duration_ms, "success",
                payload={
                    "modality": modality,
                    "template_used": result.template_used,
                    "has_reference_images": bool(reference_images),
                    "reference_image_count": len(reference_images),
                },
            )

        except Exception as exc:
            duration_ms = (time.monotonic() - start_time) * 1000.0

            logger.warning(
                "generation_optimization.ai_director.error",
                extra={
                    "reason": str(exc),
                    "fallback_action": "use_original_prompt",
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                    "duration_ms": round(duration_ms, 2),
                },
            )
            # 故障降级: 写入空结果，不阻断管线
            gen_opt = ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})
            gen_opt["ai_director"] = {
                "optimized_prompt": self._extract_prompt(ctx),
                "original_prompt": self._extract_prompt(ctx),
                "template_used": None,
                "model_used": None,
                "modality": "llm",
                "cost_usd": 0.0,
                "duration_ms": duration_ms,
                "has_reference_images": False,
                "reference_image_count": 0,
                "error": str(exc),
            }

        return ctx

    def _extract_prompt(self, ctx: PipelineContext) -> str:
        """从请求中提取用户 prompt（最后一条 user message 的 content）.

        Args:
            ctx: 管线上下文

        Returns:
            用户 prompt 字符串，若无法提取则返回空字符串
        """
        messages = ctx.request.get("messages", [])
        if not messages:
            return ""

        # 从后往前找最后一条 user message
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                # content 可能是字符串或 multimodal content 列表
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    # 提取文本部分
                    text_parts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                    return " ".join(text_parts)
                return ""

        return ""

    def _extract_reference_images(self, ctx: PipelineContext) -> List[MediaContent]:
        """从上下文中提取参考图列表.

        优先从 media_optimization 命名空间中提取已处理的媒体结果，
        若不存在则从请求的 multimodal content 中提取图片 URL。

        Args:
            ctx: 管线上下文

        Returns:
            MediaContent 列表
        """
        reference_images: List[MediaContent] = []

        # 尝试从 media_optimization 命名空间获取已检测到的图片
        media_opt = ctx.extra.get("media_optimization", {})
        per_media_results = media_opt.get("per_media_results", [])

        for result in per_media_results:
            if isinstance(result, MediaContent):
                if result.media_type == MediaType.IMAGE:
                    reference_images.append(result)
            elif isinstance(result, dict):
                # 兼容字典格式的结果
                media_type = result.get("media_type", "")
                if media_type in ("image", MediaType.IMAGE):
                    reference_images.append(
                        MediaContent(
                            media_type=MediaType.IMAGE,
                            source_url=result.get("source_url"),
                            mime_type=result.get("mime_type"),
                            size_bytes=result.get("size_bytes", 0),
                            metadata=result.get("metadata", {}),
                        )
                    )

        # 如果 media_optimization 中没有找到图片，尝试从请求中提取
        if not reference_images:
            messages = ctx.request.get("messages", [])
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "image_url":
                                image_url = part.get("image_url", {})
                                url = image_url.get("url", "") if isinstance(image_url, dict) else ""
                                if url:
                                    reference_images.append(
                                        MediaContent(
                                            media_type=MediaType.IMAGE,
                                            source_url=url,
                                        )
                                    )
                    break  # 只看最后一条 user message

        return reference_images
