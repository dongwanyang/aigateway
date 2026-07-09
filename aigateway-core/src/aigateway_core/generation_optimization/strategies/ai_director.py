"""
AI Director Strategy — AI 导演 Prompt 优化核心逻辑
==================================================

将用户模糊的提示词改写为结构化格式，包含：
- 【主体】(Subject): 主体描述
- 【动作】(Action): 动作描述
- 【环境】(Environment): 环境描述
- 【镜头】(Camera): 镜头参数描述

功能:
- 调用低成本文本模型（默认 GPT-4o-mini）进行 prompt 改写
- 输出不超过 max_prompt_length（默认 2000 字符）
- 超时处理（默认 10 秒），超时或失败时降级到原始 prompt
- 短 prompt（< min_prompt_length）自动扩展

需求: 1.1, 1.2, 1.5, 1.6
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.generation_optimization.config import AIDirectorConfig
from aigateway_core.generation_optimization.models import PromptOptimizationResult
from aigateway_core.media.types import MediaContent

logger = logging.getLogger(__name__)

# System prompt instructing the model to produce structured output
_REWRITE_SYSTEM_PROMPT = """\
你是一位专业的 AI 生成导演。你的任务是将用户提供的简短或模糊的生成提示词改写为结构化的专业提示词。

改写后的提示词必须包含以下四个部分：
【主体】详细描述画面中的主要对象、角色、物体等，包括外观、服装、表情等细节
【动作】描述主体正在进行的动作或姿态
【环境】描述场景的背景、光照、天气、时间等环境因素
【镜头】描述拍摄角度、景别、运镜方式等摄影参数

规则：
1. 保留用户原始意图，不要添加与原意无关的内容
2. 使用具体、精确的描述词汇
3. 如果原始 prompt 缺少某个维度的信息，根据上下文合理补充
4. 输出必须简洁高效，避免冗余重复
5. 直接输出改写结果，不要添加任何解释性文字
"""

_EXPAND_SYSTEM_PROMPT = """\
你是一位专业的 AI 生成导演。用户提供了一个非常简短的提示词，请根据提示词内容和参考图片的描述信息，\
将其扩展为结构化的专业提示词。

改写后的提示词必须包含以下四个部分：
【主体】详细描述画面中的主要对象、角色、物体等
【动作】描述主体正在进行的动作或姿态
【环境】描述场景的背景、光照、天气、时间等
【镜头】描述拍摄角度、景别、运镜方式等

规则：
1. 基于简短提示词进行合理的创意扩展
2. 如果有参考图片信息，从中推断风格和氛围
3. 使用具体、精确的描述词汇
4. 直接输出改写结果，不要添加任何解释性文字
"""


class AIDirectorStrategy:
    """AI 导演 — 将用户模糊提示词改写为结构化 Prompt.

    通过调用低成本文本模型（如 GPT-4o-mini），将用户的简短或模糊提示词
    改写为包含主体、动作、环境、镜头四个维度的结构化描述。

    Attributes:
        _config: AI Director 配置
        _litellm_bridge: LiteLLM 桥接层实例，用于调用文本模型
        _rewrite_prompt: 改写系统提示词（可自定义覆盖）
        _expand_prompt: 扩展系统提示词（可自定义覆盖）
    """

    # 类级别默认 prompt，子类或实例化时可覆盖
    DEFAULT_REWRITE_PROMPT = _REWRITE_SYSTEM_PROMPT
    DEFAULT_EXPAND_PROMPT = _EXPAND_SYSTEM_PROMPT

    def __init__(
        self,
        config: AIDirectorConfig,
        litellm_bridge: Any = None,
        rewrite_prompt: Optional[str] = None,
        expand_prompt: Optional[str] = None,
    ) -> None:
        """初始化 AI Director 策略.

        Args:
            config: AI Director 配置实例
            litellm_bridge: LiteLLM 桥接层实例。如果为 None，
                optimize_prompt 将直接返回原始 prompt。
            rewrite_prompt: 自定义改写系统提示词（可选，默认使用内置模板）
            expand_prompt: 自定义扩展系统提示词（可选，默认使用内置模板）
        """
        self._config = config
        self._litellm_bridge = litellm_bridge
        self._rewrite_prompt = rewrite_prompt or self.DEFAULT_REWRITE_PROMPT
        self._expand_prompt = expand_prompt or self.DEFAULT_EXPAND_PROMPT

    async def optimize_prompt(
        self,
        prompt: str,
        reference_images: List[MediaContent],
        config: AIDirectorConfig,
        ctx: PipelineContext,
    ) -> PromptOptimizationResult:
        """优化用户提示词.

        流程：
        1. 短 prompt (< min_prompt_length) 扩展：附加参考图信息
        2. 调用低成本文本模型改写为结构化格式
        3. 截断输出到 max_prompt_length
        4. 超时或失败时降级到原始 prompt

        Args:
            prompt: 用户原始提示词
            reference_images: 参考图列表
            config: AI Director 配置（允许运行时覆盖）
            ctx: 管线上下文，用于追踪和日志

        Returns:
            PromptOptimizationResult 包含优化后的 prompt 和元数据
        """
        start_time = time.monotonic()

        # 如果没有 litellm_bridge，直接返回原始 prompt
        if self._litellm_bridge is None:
            logger.warning(
                "generation_optimization.ai_director.no_bridge",
                extra={
                    "reason": "litellm_bridge not configured",
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                },
            )
            return PromptOptimizationResult(
                optimized_prompt=prompt,
                original_prompt=prompt,
                duration_ms=_elapsed_ms(start_time),
            )

        try:
            optimized = await asyncio.wait_for(
                self._do_optimize(prompt, reference_images, config, ctx),
                timeout=config.timeout_seconds,
            )

            # Truncate to max_prompt_length
            if len(optimized.optimized_prompt) > config.max_prompt_length:
                optimized.optimized_prompt = optimized.optimized_prompt[
                    : config.max_prompt_length
                ]

            optimized.duration_ms = _elapsed_ms(start_time)
            return optimized

        except asyncio.TimeoutError:
            elapsed = _elapsed_ms(start_time)
            logger.warning(
                "generation_optimization.ai_director.timeout",
                extra={
                    "reason": "timeout",
                    "fallback_action": "use_original_prompt",
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                    "duration_ms": elapsed,
                    "timeout_seconds": config.timeout_seconds,
                },
            )
            return PromptOptimizationResult(
                optimized_prompt=prompt,
                original_prompt=prompt,
                duration_ms=elapsed,
            )

        except Exception as exc:
            elapsed = _elapsed_ms(start_time)
            logger.warning(
                "generation_optimization.ai_director.error",
                extra={
                    "reason": str(exc),
                    "fallback_action": "use_original_prompt",
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                    "duration_ms": elapsed,
                },
            )
            return PromptOptimizationResult(
                optimized_prompt=prompt,
                original_prompt=prompt,
                duration_ms=elapsed,
            )

    async def _do_optimize(
        self,
        prompt: str,
        reference_images: List[MediaContent],
        config: AIDirectorConfig,
        ctx: PipelineContext,
    ) -> PromptOptimizationResult:
        """执行实际的 prompt 优化逻辑（不含超时包装）.

        Args:
            prompt: 用户原始提示词
            reference_images: 参考图列表
            config: AI Director 配置
            ctx: 管线上下文

        Returns:
            PromptOptimizationResult
        """
        # Determine if prompt is short and needs expansion
        is_short = len(prompt) < config.min_prompt_length

        # Build user message content
        user_content = self._build_user_message(
            prompt, reference_images, is_short
        )

        # Select system prompt
        system_prompt = self._expand_prompt if is_short else self._rewrite_prompt

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        # Inject trace context into downstream LLM call headers for propagation
        from aigateway_core.shared.tracing import TracingManager

        extra_headers: Dict[str, str] = {}
        TracingManager.inject_trace_context(
            headers=extra_headers,
            trace_id=ctx.trace_id,
            span_id=ctx.request_id,
        )

        # Call the low-cost text model via litellm_bridge
        response = await self._litellm_bridge.completion(
            messages=messages,
            model=config.rewrite_model,
            temperature=0.7,
            max_tokens=config.max_prompt_length,
            extra_headers=extra_headers,
        )

        # Extract the optimized prompt from the response
        optimized_text = self._extract_response_text(response)

        # If the model returned empty or error, fall back to original
        if not optimized_text:
            return PromptOptimizationResult(
                optimized_prompt=prompt,
                original_prompt=prompt,
            )

        # Extract cost from response metadata
        cost_usd = 0.0
        meta = response.get("_meta", {})
        if meta:
            cost_usd = meta.get("cost", 0.0)

        return PromptOptimizationResult(
            optimized_prompt=optimized_text,
            original_prompt=prompt,
            model_used=config.rewrite_model,
            cost_usd=cost_usd,
        )

    def _build_user_message(
        self,
        prompt: str,
        reference_images: List[MediaContent],
        is_short: bool,
    ) -> str:
        """构建发送给改写模型的用户消息.

        对于短 prompt，附加参考图的元数据信息以提供上下文。

        Args:
            prompt: 用户原始提示词
            reference_images: 参考图列表
            is_short: 是否为短 prompt

        Returns:
            构建好的用户消息文本
        """
        parts = [f"请改写以下提示词：\n{prompt}"]

        if is_short and reference_images:
            # Append contextual hints from reference images
            hints = self._extract_image_hints(reference_images)
            if hints:
                parts.append(f"\n参考图片信息：\n{hints}")

        return "\n".join(parts)

    def _extract_image_hints(self, reference_images: List[MediaContent]) -> str:
        """从参考图中提取上下文提示信息.

        提取参考图的元数据（如描述、标签等）用于辅助短 prompt 扩展。

        Args:
            reference_images: 参考图列表

        Returns:
            参考图提示信息文本
        """
        hints: List[str] = []
        for i, img in enumerate(reference_images, 1):
            img_info_parts: List[str] = []

            # Use media type
            if img.media_type:
                img_info_parts.append(f"类型: {img.media_type.value}")

            # Use mime type
            if img.mime_type:
                img_info_parts.append(f"格式: {img.mime_type}")

            # Extract metadata hints (description, tags, style, etc.)
            if img.metadata:
                desc = img.metadata.get("description", "")
                if desc:
                    img_info_parts.append(f"描述: {desc}")
                tags = img.metadata.get("tags", [])
                if tags:
                    img_info_parts.append(f"标签: {', '.join(tags)}")
                style = img.metadata.get("style", "")
                if style:
                    img_info_parts.append(f"风格: {style}")

            # Use extracted text if available
            if img.extracted_text:
                img_info_parts.append(f"内容: {img.extracted_text}")

            if img_info_parts:
                hints.append(f"图片{i}: {'; '.join(img_info_parts)}")

        return "\n".join(hints)

    def _extract_response_text(self, response: Dict[str, Any]) -> str:
        """从 LiteLLM Bridge 响应中提取文本内容.

        Args:
            response: LiteLLM Bridge 返回的响应字典

        Returns:
            提取的文本内容，如果提取失败则返回空字符串
        """
        # Check for error in response
        if "error" in response:
            return ""

        # Extract from standard OpenAI response format
        data = response.get("data", response)
        choices = data.get("choices", [])
        if not choices:
            return ""

        first_choice = choices[0]
        message = first_choice.get("message", {})
        content = message.get("content", "")

        return content.strip() if content else ""

    async def apply_template(
        self,
        template_name: str,
        variables: Dict[str, str],
        user_id: str,
    ) -> str:
        """应用提示词模板.

        占位符：将在 task 4.3 中连接 PromptTemplateManager 完成完整实现。
        当前为 placeholder 实现。

        Args:
            template_name: 模板名称
            variables: 模板占位符变量映射
            user_id: 用户/API Key 标识

        Returns:
            渲染后的 prompt 文本
        """
        # Placeholder: will be connected to PromptTemplateManager in task 4.3
        logger.info(
            "apply_template called (placeholder): template=%s, user=%s",
            template_name,
            user_id,
        )
        return ""


def _elapsed_ms(start_time: float) -> float:
    """计算从 start_time 到当前的经过毫秒数."""
    return (time.monotonic() - start_time) * 1000.0
