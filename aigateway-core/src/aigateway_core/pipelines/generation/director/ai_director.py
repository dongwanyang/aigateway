"""
AI Director Strategy — AI 导演 Prompt 优化核心逻辑
==================================================

将用户模糊的提示词改写为结构化格式，并为视频请求生成彼此独立的
关键帧提示词与运动提示词。

功能:
- 调用低成本文本模型（默认 GPT-4o-mini）进行 prompt 改写
- 保留用户提示词的主要语言，不做全局英文强制转换
- 视频请求输出可验证的 VideoGenerationPlan
- 输出不超过 max_prompt_length（默认 2000 字符）
- 超时、空响应或非法 JSON 时安全降级并记录 fallback_reason
- 短 prompt（< min_prompt_length）自动扩展

需求: 1.1, 1.2, 1.5, 1.6
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.pipelines.generation._common.config import AIDirectorConfig
from aigateway_core.pipelines.generation._common.models import (
    PromptOptimizationResult,
    VideoGenerationPlan,
)
from aigateway_core.prefix.media.types import MediaContent

logger = logging.getLogger(__name__)

_REWRITE_SYSTEM_PROMPT = """\
你是一位专业的 AI 生成导演。你的任务是将用户提供的简短或模糊的生成提示词改写为结构化的专业提示词。

改写后的提示词必须包含以下四个部分：
Subject: 详细描述画面中的主要对象、角色、物体等，包括外观、服装、表情等细节
Action: 描述主体正在进行的动作或姿态
Environment: 描述场景的背景、光照、天气、时间等环境因素
Camera: 描述拍摄角度、景别、运镜方式等摄影参数

规则：
1. 保留用户原始意图，不要添加与原意无关的内容
2. 使用具体、精确的描述词汇
3. 如果原始 prompt 缺少某个维度的信息，根据上下文合理补充
4. 输出必须简洁高效，避免冗余重复
5. 直接输出改写结果，不要添加任何解释性文字
6. 输出语言必须与用户提示词的主要语言一致；不得因为兼容性全局要求必须使用自然、准确的英文。只有调用方明确说明目标模型不支持用户语言时，才允许显式回退到目标模型支持的语言
7. 如果用户要求画面中出现特定文字，只将该文字原样保留，不要翻译
"""

_EXPAND_SYSTEM_PROMPT = """\
你是一位专业的 AI 生成导演。用户提供了一个非常简短的提示词，请根据提示词内容和参考图片的描述信息，\
将其扩展为结构化的专业提示词。

改写后的提示词必须包含以下四个部分：
Subject: 详细描述画面中的主要对象、角色、物体等
Action: 描述主体正在进行的动作或姿态
Environment: 描述场景的背景、光照、天气、时间等
Camera: 描述拍摄角度、景别、运镜方式等

规则：
1. 基于简短提示词进行合理的创意扩展
2. 如果有参考图片信息，从中推断风格和氛围
3. 使用具体、精确的描述词汇
4. 直接输出改写结果，不要添加任何解释性文字
5. 输出语言必须与用户提示词的主要语言一致；不得因为兼容性全局要求必须使用自然、准确的英文。只有调用方明确说明目标模型不支持用户语言时，才允许显式回退到目标模型支持的语言
6. 如果用户要求画面中出现特定文字，只将该文字原样保留，不要翻译
"""

_VIDEO_PLAN_SYSTEM_PROMPT = """\
你是一位专业的视频生成导演。请把用户的视频请求拆分为关键帧描述和运动描述，并只输出一个 JSON 对象，不要输出 Markdown、代码块或解释。

JSON 必须包含：
{
  "keyframe_prompt": "只描述视频起始关键帧的静态画面，包括主体外观、姿态、场景、光线和构图，不描述后续运动",
  "motion_prompt": "只描述主体动作、镜头运动、速度与时间连续性，并要求保持主体身份、脸部、颜色、比例和场景一致，不切换场景",
  "duration_seconds": 5,
  "language": "zh"
}

规则：
1. keyframe_prompt 和 motion_prompt 必须分工明确，禁止把完整动作过程写入关键帧提示词
2. 保留用户提示词的主要语言；中文请求输出中文，英文请求输出英文
3. 不得改变用户指定的主体、外观、数量、场景或文字
4. motion_prompt 必须包含主体与场景一致性约束
5. duration_seconds 使用调用方提供的时长，不自行扩展或缩短
6. language 使用简短语言代码，例如 zh 或 en
"""

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


class AIDirectorStrategy:
    """AI 导演 — 优化普通生成提示词并构建视频提示词计划."""

    DEFAULT_REWRITE_PROMPT = _REWRITE_SYSTEM_PROMPT
    DEFAULT_EXPAND_PROMPT = _EXPAND_SYSTEM_PROMPT
    DEFAULT_VIDEO_PLAN_PROMPT = _VIDEO_PLAN_SYSTEM_PROMPT

    def __init__(
        self,
        config: AIDirectorConfig,
        litellm_bridge: Any = None,
        rewrite_prompt: str | None = None,
        expand_prompt: str | None = None,
        model_selector: Any = None,
        video_plan_prompt: str | None = None,
    ) -> None:
        self._config = config
        self._litellm_bridge = litellm_bridge
        self._rewrite_prompt = rewrite_prompt or self.DEFAULT_REWRITE_PROMPT
        self._expand_prompt = expand_prompt or self.DEFAULT_EXPAND_PROMPT
        self._video_plan_prompt = video_plan_prompt or self.DEFAULT_VIDEO_PLAN_PROMPT
        self._model_selector = model_selector

    async def optimize_prompt(
        self,
        prompt: str,
        reference_images: list[MediaContent],
        config: AIDirectorConfig,
        ctx: PipelineContext,
    ) -> PromptOptimizationResult:
        """优化普通生成提示词，并保留用户提示词的主要语言."""
        start_time = time.monotonic()

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

            if len(optimized.optimized_prompt) > config.max_prompt_length:
                optimized.optimized_prompt = optimized.optimized_prompt[
                    : config.max_prompt_length
                ]

            optimized.duration_ms = _elapsed_ms(start_time)
            return optimized

        except TimeoutError:
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

    async def build_video_generation_plan(
        self,
        prompt: str,
        reference_images: list[MediaContent],
        config: AIDirectorConfig,
        ctx: PipelineContext,
        *,
        duration_seconds: float = 5.0,
        fps: int = 8,
        source_draft_id: str | None = None,
        source_image_sha256: str | None = None,
    ) -> VideoGenerationPlan:
        """构建视频关键帧/运动提示词计划，失败时返回可追踪的安全降级计划."""
        _validate_video_timing(duration_seconds, fps)
        language = _detect_prompt_language(prompt)

        if self._litellm_bridge is None:
            return self._fallback_video_plan(
                prompt,
                language,
                duration_seconds,
                fps,
                source_draft_id,
                source_image_sha256,
                "no_bridge",
            )

        try:
            return await asyncio.wait_for(
                self._do_build_video_generation_plan(
                    prompt=prompt,
                    reference_images=reference_images,
                    config=config,
                    ctx=ctx,
                    duration_seconds=duration_seconds,
                    fps=fps,
                    source_draft_id=source_draft_id,
                    source_image_sha256=source_image_sha256,
                    language=language,
                ),
                timeout=config.timeout_seconds,
            )
        except TimeoutError:
            fallback_reason = "timeout"
        except ValueError as exc:
            fallback_reason = str(exc) or "invalid_video_plan"
        except Exception as exc:
            logger.warning(
                "generation_optimization.ai_director.video_plan_error",
                extra={
                    "reason": str(exc),
                    "fallback_action": "use_safe_video_plan",
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                },
            )
            fallback_reason = "provider_error"

        logger.warning(
            "generation_optimization.ai_director.video_plan_fallback",
            extra={
                "reason": fallback_reason,
                "request_id": ctx.request_id,
                "trace_id": ctx.trace_id,
                "prompt_language": language,
            },
        )
        return self._fallback_video_plan(
            prompt,
            language,
            duration_seconds,
            fps,
            source_draft_id,
            source_image_sha256,
            fallback_reason,
        )

    async def _do_optimize(
        self,
        prompt: str,
        reference_images: list[MediaContent],
        config: AIDirectorConfig,
        ctx: PipelineContext,
    ) -> PromptOptimizationResult:
        is_short = len(prompt) < config.min_prompt_length
        user_content = self._build_user_message(prompt, reference_images, is_short)
        system_prompt = self._expand_prompt if is_short else self._rewrite_prompt
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        rewrite_model = await self._select_rewrite_model(config)
        response = await self._complete(messages, rewrite_model, config, ctx)
        optimized_text = self._extract_response_text(response)
        if not optimized_text:
            return PromptOptimizationResult(
                optimized_prompt=prompt,
                original_prompt=prompt,
            )

        meta = response.get("_meta", {})
        cost_usd = meta.get("cost", 0.0) if isinstance(meta, dict) else 0.0
        return PromptOptimizationResult(
            optimized_prompt=optimized_text,
            original_prompt=prompt,
            model_used=rewrite_model,
            cost_usd=cost_usd,
        )

    async def _do_build_video_generation_plan(
        self,
        *,
        prompt: str,
        reference_images: list[MediaContent],
        config: AIDirectorConfig,
        ctx: PipelineContext,
        duration_seconds: float,
        fps: int,
        source_draft_id: str | None,
        source_image_sha256: str | None,
        language: str,
    ) -> VideoGenerationPlan:
        hints = self._extract_image_hints(reference_images)
        user_parts = [
            f"用户视频请求：\n{prompt}",
            f"请求时长：{duration_seconds:g} 秒",
            f"目标语言：{language}",
        ]
        if hints:
            user_parts.append(f"参考图片信息：\n{hints}")
        messages = [
            {"role": "system", "content": self._video_plan_prompt},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ]
        rewrite_model = await self._select_rewrite_model(config)
        response = await self._complete(messages, rewrite_model, config, ctx)
        response_text = self._extract_response_text(response)
        if not response_text:
            raise ValueError("empty_response")

        payload = _parse_json_object(response_text)
        keyframe_prompt = _required_plan_text(payload, "keyframe_prompt")
        motion_prompt = _required_plan_text(payload, "motion_prompt")
        response_language = str(payload.get("language") or language).strip().lower()
        if response_language not in {"zh", "en"}:
            response_language = language
        motion_prompt = _ensure_consistency_constraint(
            motion_prompt,
            response_language,
        )
        return VideoGenerationPlan(
            source_prompt=prompt,
            keyframe_prompt=keyframe_prompt,
            motion_prompt=motion_prompt,
            prompt_language=response_language,
            duration_seconds=float(duration_seconds),
            fps=int(fps),
            frame_count=max(1, round(duration_seconds * fps)),
            source_draft_id=source_draft_id,
            source_image_sha256=source_image_sha256,
        )

    async def _select_rewrite_model(self, config: AIDirectorConfig) -> str:
        rewrite_model = config.rewrite_model
        if self._model_selector is not None:
            try:
                rewrite_model = await self._model_selector.select_text_model()
            except Exception as exc:
                logger.warning(
                    "ai_director: model_selector failed %s, falling back to config.rewrite_model",
                    exc,
                )
        return rewrite_model

    async def _complete(
        self,
        messages: list[dict[str, str]],
        rewrite_model: str,
        config: AIDirectorConfig,
        ctx: PipelineContext,
    ) -> dict[str, Any]:
        from aigateway_core.shared.tracing import TracingManager

        extra_headers: dict[str, str] = {}
        TracingManager.inject_trace_context(
            headers=extra_headers,
            trace_id=ctx.trace_id,
            span_id=ctx.request_id,
        )
        return await self._litellm_bridge.completion(
            messages=messages,
            model=rewrite_model,
            temperature=0.7,
            max_tokens=config.max_prompt_length,
            extra_headers=extra_headers,
            intent="understanding",
        )

    def _fallback_video_plan(
        self,
        prompt: str,
        language: str,
        duration_seconds: float,
        fps: int,
        source_draft_id: str | None,
        source_image_sha256: str | None,
        fallback_reason: str,
    ) -> VideoGenerationPlan:
        return VideoGenerationPlan(
            source_prompt=prompt,
            keyframe_prompt=prompt,
            motion_prompt=_ensure_consistency_constraint(prompt, language),
            prompt_language=language,
            duration_seconds=float(duration_seconds),
            fps=int(fps),
            frame_count=max(1, round(duration_seconds * fps)),
            source_draft_id=source_draft_id,
            source_image_sha256=source_image_sha256,
            fallback_reason=fallback_reason,
        )

    def _build_user_message(
        self,
        prompt: str,
        reference_images: list[MediaContent],
        is_short: bool,
    ) -> str:
        parts = [f"请改写以下提示词：\n{prompt}"]
        if is_short and reference_images:
            hints = self._extract_image_hints(reference_images)
            if hints:
                parts.append(f"\n参考图片信息：\n{hints}")
        return "\n".join(parts)

    def _extract_image_hints(self, reference_images: list[MediaContent]) -> str:
        hints: list[str] = []
        for i, img in enumerate(reference_images, 1):
            img_info_parts: list[str] = []
            if img.media_type:
                img_info_parts.append(f"类型: {img.media_type.value}")
            if img.mime_type:
                img_info_parts.append(f"格式: {img.mime_type}")
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
            if img.extracted_text:
                img_info_parts.append(f"内容: {img.extracted_text}")
            if img_info_parts:
                hints.append(f"图片{i}: {'; '.join(img_info_parts)}")
        return "\n".join(hints)

    def _extract_response_text(self, response: dict[str, Any]) -> str:
        if "error" in response:
            return ""
        data = response.get("data", response)
        if not isinstance(data, dict):
            return ""
        choices = data.get("choices", [])
        if not isinstance(choices, list) or not choices:
            return ""
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            return ""
        message = first_choice.get("message", {})
        if not isinstance(message, dict):
            return ""
        content = message.get("content", "")
        return content.strip() if isinstance(content, str) else ""

    async def apply_template(
        self,
        template_name: str,
        variables: dict[str, str],
        user_id: str,
    ) -> str:
        """应用提示词模板（placeholder）."""
        logger.info(
            "apply_template called (placeholder): template=%s, user=%s",
            template_name,
            user_id,
        )
        return ""


def _detect_prompt_language(prompt: str) -> str:
    return "zh" if _CJK_RE.search(prompt) else "en"


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = _CODE_FENCE_RE.sub("", text.strip()).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid_json") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid_json_object")
    return payload


def _required_plan_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing_{key}")
    return value.strip()


def _ensure_consistency_constraint(motion_prompt: str, language: str) -> str:
    if language == "zh":
        constraint = "保持主体身份、脸部、颜色、身体比例和场景一致，不切换场景。"
        markers = ("保持主体", "场景一致", "不切换场景")
    else:
        constraint = (
            "Keep the subject identity, face, colors, body proportions, and scene "
            "consistent; do not switch scenes."
        )
        markers = ("subject identity", "scene consistent", "do not switch scenes")
    lowered = motion_prompt.lower()
    if any(marker.lower() in lowered for marker in markers):
        return motion_prompt
    separator = (
        ""
        if motion_prompt.endswith(("。", ".", "!", "！", "?", "？"))
        else "。"
        if language == "zh"
        else "."
    )
    return f"{motion_prompt}{separator} {constraint}".strip()


def _validate_video_timing(duration_seconds: float, fps: int) -> None:
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be greater than zero")
    if fps <= 0:
        raise ValueError("fps must be greater than zero")


def _elapsed_ms(start_time: float) -> float:
    return (time.monotonic() - start_time) * 1000.0
