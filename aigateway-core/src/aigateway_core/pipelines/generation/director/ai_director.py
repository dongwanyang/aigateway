"""
AI Director Strategy — language-aware image and video prompt planning.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections.abc import Iterable
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
6. 输出语言由调用方提供的目标模型语言策略决定；目标模型支持用户主要语言时保留该语言，否则显式转换为目标模型支持的语言
7. 用户要求画面中出现的特定文字必须原样保留，不要翻译
"""

_EXPAND_SYSTEM_PROMPT = """\
你是一位专业的 AI 生成导演。用户提供了一个非常简短的提示词，请根据提示词内容和参考图片描述，将其扩展为结构化的专业提示词。

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
5. 输出语言由调用方提供的目标模型语言策略决定；目标模型支持用户主要语言时保留该语言，否则显式转换为目标模型支持的语言
6. 用户要求画面中出现的特定文字必须原样保留，不要翻译
"""

_VIDEO_PLAN_SYSTEM_PROMPT = """\
你是一位专业的视频生成导演。请把用户的视频请求拆分为关键帧描述和运动描述，并只输出一个 JSON 对象，不要输出 Markdown、代码块或解释。

JSON 必须包含：
{
  "keyframe_prompt": "只描述起始关键帧的静态画面，不描述后续运动",
  "motion_prompt": "只描述主体动作、镜头运动、速度和时间连续性",
  "language": "用户原始提示词的主要语言代码"
}

规则：
1. keyframe_prompt 只描述主体外观、静态姿态、场景、光线和构图
2. motion_prompt 只描述动作与运镜，并包含主体身份、脸部、颜色、身体比例和场景一致性约束
3. keyframe_prompt 和 motion_prompt 分别使用调用方指定的语言
4. 不得改变用户指定的主体、外观、数量、场景或画面文字
5. 用户要求画面中出现的特定文字必须原样保留
6. 不要自行改变时长或帧率
"""

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_QUOTED_LITERAL_RE = re.compile(
    r'"[^"]*"|\'[^\']*\'|“[^”]*”|‘[^’]*’',
    re.DOTALL,
)
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[。！？.!?])\s*|[，,；;]\s*")
_SUPPORTED_VIDEO_DURATIONS = (3.0, 5.0, 8.0)
_MAX_VIDEO_FPS = 60
_MAX_VIDEO_FRAMES = 480

_ZH_MOTION_RE = re.compile(
    r"跑|奔|行走|移动|飞|跳|旋转|摇尾|挥手|抬起|落下|冲向|追逐|"
    r"推近|拉远|环绕|跟随|平移|摇摄|变焦|快速|缓慢|逐渐|秒"
)
_EN_MOTION_RE = re.compile(
    r"\b(run(?:s|ning)?|walk(?:s|ing)?|move(?:s|ment|ing)?|fly(?:ing|ies)?|"
    r"jump(?:s|ing)?|turn(?:s|ing)?|spin(?:s|ning)?|wag(?:s|ging)?|wave(?:s|ing)?|"
    r"zoom|pan|track(?:s|ing)?|dolly|orbit|quickly|slowly|gradually|seconds?)\b",
    re.IGNORECASE,
)


class AIDirectorStrategy:
    """Optimize image prompts and build model-aware video prompt plans."""

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
        *,
        target_languages: Iterable[str] | None = None,
        source_language: str | None = None,
    ) -> PromptOptimizationResult:
        """Optimize a prompt using the target model's declared languages."""
        start_time = time.monotonic()
        detected_language = _normalize_language_code(
            source_language or _detect_prompt_language(prompt)
        )
        languages = _normalize_target_languages(target_languages, default=("en",))
        output_language, fallback_reason = _select_output_language(
            detected_language, languages
        )

        if self._litellm_bridge is None:
            return PromptOptimizationResult(
                optimized_prompt=prompt,
                original_prompt=prompt,
                duration_ms=_elapsed_ms(start_time),
                source_language=detected_language,
                output_language=detected_language,
                language_fallback_reason=(
                    "no_bridge"
                    if output_language != detected_language
                    else None
                ),
            )

        try:
            optimized = await asyncio.wait_for(
                self._do_optimize(
                    prompt,
                    reference_images,
                    config,
                    ctx,
                    source_language=detected_language,
                    output_language=output_language,
                    language_fallback_reason=fallback_reason,
                ),
                timeout=config.timeout_seconds,
            )
            if len(optimized.optimized_prompt) > config.max_prompt_length:
                optimized.optimized_prompt = optimized.optimized_prompt[
                    : config.max_prompt_length
                ]
            optimized.duration_ms = _elapsed_ms(start_time)
            return optimized
        except TimeoutError:
            reason = "timeout"
        except ValueError as exc:
            reason = str(exc) or "invalid_prompt_response"
        except Exception as exc:
            logger.warning(
                "generation_optimization.ai_director.error",
                extra={
                    "reason": str(exc),
                    "fallback_action": "use_original_prompt",
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                },
            )
            reason = "provider_error"

        return PromptOptimizationResult(
            optimized_prompt=prompt,
            original_prompt=prompt,
            duration_ms=_elapsed_ms(start_time),
            source_language=detected_language,
            output_language=detected_language,
            language_fallback_reason=(
                reason
                if output_language == detected_language
                else f"{fallback_reason or 'target_language_conversion'}:{reason}"
            ),
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
        source_language: str | None = None,
        keyframe_languages: Iterable[str] | None = None,
        motion_languages: Iterable[str] | None = None,
    ) -> VideoGenerationPlan:
        """Build a keyframe/motion plan with independent model language policies."""
        duration, normalized_fps, frame_count = _normalize_video_timing(
            duration_seconds, fps
        )
        language = _normalize_language_code(
            source_language or _detect_prompt_language(prompt)
        )
        keyframe_targets = _normalize_target_languages(
            keyframe_languages, default=("en",)
        )
        motion_targets = _normalize_target_languages(
            motion_languages, default=("zh", "en")
        )
        keyframe_language, keyframe_fallback = _select_output_language(
            language, keyframe_targets
        )
        motion_language, motion_fallback = _select_output_language(
            language, motion_targets
        )
        language_fallback_reason = _join_reasons(
            (
                f"keyframe:{keyframe_fallback}" if keyframe_fallback else None,
                f"motion:{motion_fallback}" if motion_fallback else None,
            )
        )

        if self._litellm_bridge is None:
            return self._fallback_video_plan(
                prompt=prompt,
                source_language=language,
                keyframe_language=keyframe_language,
                motion_language=motion_language,
                duration_seconds=duration,
                fps=normalized_fps,
                frame_count=frame_count,
                source_draft_id=source_draft_id,
                source_image_sha256=source_image_sha256,
                fallback_reason="no_bridge",
                language_fallback_reason=language_fallback_reason,
            )

        try:
            return await asyncio.wait_for(
                self._do_build_video_generation_plan(
                    prompt=prompt,
                    reference_images=reference_images,
                    config=config,
                    ctx=ctx,
                    duration_seconds=duration,
                    fps=normalized_fps,
                    frame_count=frame_count,
                    source_draft_id=source_draft_id,
                    source_image_sha256=source_image_sha256,
                    source_language=language,
                    keyframe_language=keyframe_language,
                    motion_language=motion_language,
                    language_fallback_reason=language_fallback_reason,
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
                "source_language": language,
                "keyframe_language": keyframe_language,
                "motion_language": motion_language,
            },
        )
        return self._fallback_video_plan(
            prompt=prompt,
            source_language=language,
            keyframe_language=keyframe_language,
            motion_language=motion_language,
            duration_seconds=duration,
            fps=normalized_fps,
            frame_count=frame_count,
            source_draft_id=source_draft_id,
            source_image_sha256=source_image_sha256,
            fallback_reason=fallback_reason,
            language_fallback_reason=language_fallback_reason,
        )

    async def _do_optimize(
        self,
        prompt: str,
        reference_images: list[MediaContent],
        config: AIDirectorConfig,
        ctx: PipelineContext,
        *,
        source_language: str,
        output_language: str,
        language_fallback_reason: str | None,
    ) -> PromptOptimizationResult:
        is_short = len(prompt) < config.min_prompt_length
        user_content = self._build_user_message(prompt, reference_images, is_short)
        base_system_prompt = self._expand_prompt if is_short else self._rewrite_prompt
        system_prompt = (
            f"{base_system_prompt}\n\n"
            f"语言策略：用户主要语言为 {source_language}；目标模型支持的输出语言"
            f"已选择为 {output_language}。视觉描述与四个标题必须使用"
            f" {output_language}；引号内要求出现在画面中的文字保持原样。"
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        rewrite_model = await self._select_rewrite_model(config)
        response = await self._complete(messages, rewrite_model, config, ctx)
        optimized_text = self._extract_response_text(response)
        if not optimized_text:
            raise ValueError("empty_response")

        if (
            output_language in {"zh", "en", "ja"}
            and _detect_prompt_language(optimized_text) != output_language
        ):
            correction_messages = [
                *messages,
                {"role": "assistant", "content": optimized_text},
                {
                    "role": "user",
                    "content": (
                        f"上一个结果没有使用要求的 {output_language}。请严格改为"
                        f" {output_language}，保留引号内文字，只输出最终结构化提示词。"
                    ),
                },
            ]
            corrected = await self._complete(
                correction_messages, rewrite_model, config, ctx
            )
            corrected_text = self._extract_response_text(corrected)
            if corrected_text:
                optimized_text = corrected_text
                response = corrected

        meta = response.get("_meta", {})
        cost_usd = meta.get("cost", 0.0) if isinstance(meta, dict) else 0.0
        return PromptOptimizationResult(
            optimized_prompt=optimized_text,
            original_prompt=prompt,
            model_used=rewrite_model,
            cost_usd=float(cost_usd or 0.0),
            source_language=source_language,
            output_language=output_language,
            language_fallback_reason=language_fallback_reason,
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
        frame_count: int,
        source_draft_id: str | None,
        source_image_sha256: str | None,
        source_language: str,
        keyframe_language: str,
        motion_language: str,
        language_fallback_reason: str | None,
    ) -> VideoGenerationPlan:
        hints = self._extract_image_hints(reference_images)
        user_parts = [
            f"用户视频请求：\n{prompt}",
            f"原始主要语言：{source_language}",
            f"关键帧必须使用：{keyframe_language}",
            f"运动提示词必须使用：{motion_language}",
            f"请求时长：{duration_seconds:g} 秒",
            f"目标帧率：{fps}",
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
        if _detect_prompt_language(keyframe_prompt) != keyframe_language:
            raise ValueError("keyframe_language_mismatch")
        if _detect_prompt_language(motion_prompt) != motion_language:
            raise ValueError("motion_language_mismatch")
        motion_prompt = _ensure_consistency_constraint(
            motion_prompt, motion_language
        )
        if _contains_motion_description(keyframe_prompt, keyframe_language):
            raise ValueError("keyframe_contains_motion")

        meta = response.get("_meta", {})
        cost_usd = meta.get("cost", 0.0) if isinstance(meta, dict) else 0.0
        return VideoGenerationPlan(
            source_prompt=prompt,
            keyframe_prompt=keyframe_prompt,
            motion_prompt=motion_prompt,
            prompt_language=source_language,
            keyframe_language=keyframe_language,
            motion_language=motion_language,
            duration_seconds=duration_seconds,
            fps=fps,
            frame_count=frame_count,
            source_draft_id=source_draft_id,
            source_image_sha256=source_image_sha256,
            language_fallback_reason=language_fallback_reason,
            model_used=rewrite_model,
            cost_usd=float(cost_usd or 0.0),
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
        result = await self._litellm_bridge.completion(
            messages=messages,
            model=rewrite_model,
            temperature=0.7,
            max_tokens=config.max_prompt_length,
            extra_headers=extra_headers,
            intent="understanding",
        )
        return result if isinstance(result, dict) else {}

    def _fallback_video_plan(
        self,
        *,
        prompt: str,
        source_language: str,
        keyframe_language: str,
        motion_language: str,
        duration_seconds: float,
        fps: int,
        frame_count: int,
        source_draft_id: str | None,
        source_image_sha256: str | None,
        fallback_reason: str,
        language_fallback_reason: str | None,
    ) -> VideoGenerationPlan:
        keyframe_prompt, motion_prompt = _split_fallback_video_prompt(
            prompt,
            source_language=source_language,
            keyframe_language=keyframe_language,
            motion_language=motion_language,
        )
        return VideoGenerationPlan(
            source_prompt=prompt,
            keyframe_prompt=keyframe_prompt,
            motion_prompt=_ensure_consistency_constraint(
                motion_prompt, motion_language
            ),
            prompt_language=source_language,
            keyframe_language=keyframe_language,
            motion_language=motion_language,
            duration_seconds=duration_seconds,
            fps=fps,
            frame_count=frame_count,
            source_draft_id=source_draft_id,
            source_image_sha256=source_image_sha256,
            fallback_reason=fallback_reason,
            language_fallback_reason=language_fallback_reason,
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
        for index, image in enumerate(reference_images, 1):
            info: list[str] = []
            if image.media_type:
                info.append(f"类型: {image.media_type.value}")
            if image.mime_type:
                info.append(f"格式: {image.mime_type}")
            if image.metadata:
                description = image.metadata.get("description", "")
                if description:
                    info.append(f"描述: {description}")
                tags = image.metadata.get("tags", [])
                if tags:
                    info.append(f"标签: {', '.join(map(str, tags))}")
                style = image.metadata.get("style", "")
                if style:
                    info.append(f"风格: {style}")
            if image.extracted_text:
                info.append(f"内容: {image.extracted_text}")
            if info:
                hints.append(f"图片{index}: {'; '.join(info)}")
        return "\n".join(hints)

    def _extract_response_text(self, response: dict[str, Any]) -> str:
        if not isinstance(response, dict) or "error" in response:
            return ""
        data = response.get("data", response)
        if not isinstance(data, dict):
            return ""
        choices = data.get("choices", [])
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        message = first.get("message", {})
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
        logger.info(
            "apply_template called (placeholder): template=%s, user=%s",
            template_name,
            user_id,
        )
        return ""


def _normalize_language_code(language: str | None) -> str:
    value = str(language or "").strip().lower().replace("_", "-")
    aliases = {
        "zh-cn": "zh",
        "zh-sg": "zh",
        "zh-hans": "zh",
        "zh-tw": "zh",
        "zh-hant": "zh",
        "en-us": "en",
        "en-gb": "en",
        "ja-jp": "ja",
    }
    return aliases.get(value, value or "en")


def _detect_prompt_language(prompt: str) -> str:
    text = _QUOTED_LITERAL_RE.sub("", str(prompt or ""))
    kana_count = len(_KANA_RE.findall(text))
    cjk_count = len(_CJK_RE.findall(text))
    latin_count = len(_LATIN_RE.findall(text))
    if kana_count:
        return "ja"
    if cjk_count and cjk_count * 2 >= max(1, latin_count):
        return "zh"
    return "en"


def _normalize_target_languages(
    languages: Iterable[str] | None,
    *,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    if isinstance(languages, str):
        raw = (languages,)
    else:
        raw = tuple(languages) if languages is not None else default
    result: list[str] = []
    for item in raw:
        code = _normalize_language_code(str(item))
        if code and code not in result:
            result.append(code)
    return tuple(result or default)


def _select_output_language(
    source_language: str,
    target_languages: tuple[str, ...],
) -> tuple[str, str | None]:
    source = _normalize_language_code(source_language)
    if source in target_languages:
        return source, None
    return target_languages[0], "target_model_language_unsupported"


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
        canonical = "保持主体身份、脸部、颜色、身体比例和场景一致，不切换场景。"
        groups = (
            ("主体身份", "保持主体"),
            ("脸部", "面部"),
            ("颜色", "色彩", "毛色"),
            ("身体比例", "比例"),
            ("场景一致",),
            ("不切换场景", "不换场景"),
        )
    else:
        canonical = (
            "Keep the subject identity, face, colors, body proportions, and scene "
            "consistent; do not switch scenes."
        )
        groups = (
            ("subject identity", "same subject"),
            ("face", "facial"),
            ("color", "colour"),
            ("body proportion", "proportion"),
            ("scene consistent", "consistent scene"),
            ("do not switch scenes", "no scene changes"),
        )
    lowered = motion_prompt.lower()
    complete = all(
        any(marker.lower() in lowered for marker in alternatives)
        for alternatives in groups
    )
    if complete:
        return motion_prompt
    separator = (
        ""
        if motion_prompt.endswith(("。", ".", "!", "！", "?", "？"))
        else "。"
        if language == "zh"
        else "."
    )
    return f"{motion_prompt}{separator} {canonical}".strip()


def _contains_motion_description(text: str, language: str) -> bool:
    return bool(
        _ZH_MOTION_RE.search(text)
        if language == "zh"
        else _EN_MOTION_RE.search(text)
    )


def _split_fallback_video_prompt(
    prompt: str,
    *,
    source_language: str,
    keyframe_language: str,
    motion_language: str,
) -> tuple[str, str]:
    clauses = [
        part.strip()
        for part in _CLAUSE_SPLIT_RE.split(prompt)
        if part and part.strip()
    ]
    motion_re = _ZH_MOTION_RE if source_language == "zh" else _EN_MOTION_RE
    static_clauses = [part for part in clauses if not motion_re.search(part)]
    motion_clauses = [part for part in clauses if motion_re.search(part)]

    if keyframe_language == source_language and static_clauses:
        keyframe = "，".join(static_clauses) if source_language == "zh" else ", ".join(static_clauses)
    elif keyframe_language == "zh":
        keyframe = "用户要求的主要主体位于指定场景中，保持动作开始前的静止姿态。"
    else:
        keyframe = (
            "The requested main subject in the requested scene, shown in the "
            "initial static pose before any motion begins."
        )

    if motion_language == source_language:
        motion = (
            ("，".join(motion_clauses) if source_language == "zh" else ", ".join(motion_clauses))
            or prompt
        )
    elif motion_language == "zh":
        motion = "执行用户要求的动作和运镜，并保持时间连续。"
    else:
        motion = "Perform the requested action and camera movement with continuous timing."

    return keyframe, motion


def _normalize_video_timing(
    duration_seconds: float,
    fps: int,
) -> tuple[float, int, int]:
    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, (int, float))
        or not math.isfinite(float(duration_seconds))
    ):
        raise ValueError("duration_seconds must be finite")
    duration = float(duration_seconds)
    if not any(math.isclose(duration, allowed) for allowed in _SUPPORTED_VIDEO_DURATIONS):
        raise ValueError("video_duration_unsupported")
    if isinstance(fps, bool) or not isinstance(fps, int):
        raise ValueError("fps must be an integer")
    if fps <= 0 or fps > _MAX_VIDEO_FPS:
        raise ValueError("fps_out_of_range")
    frame_count = round(duration * fps)
    if frame_count <= 0 or frame_count > _MAX_VIDEO_FRAMES:
        raise ValueError("frame_count_out_of_range")
    return duration, fps, frame_count


def _join_reasons(reasons: Iterable[str | None]) -> str | None:
    values = [reason for reason in reasons if reason]
    return ";".join(values) if values else None


def _elapsed_ms(start_time: float) -> float:
    return (time.monotonic() - start_time) * 1000.0
