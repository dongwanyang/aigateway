"""
AIDirectorPlugin — model-aware prompt optimization and video planning.
"""
from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.pipelines.generation._common.config import (
    GenerationOptimizationConfig,
)
from aigateway_core.pipelines.generation.director.ai_director import (
    AIDirectorStrategy,
)
from aigateway_core.prefix.media.types import MediaContent, MediaType

NS_GENERATION_OPTIMIZATION = "generation_optimization"

_PRESET_LANGUAGES: dict[str, tuple[str, ...]] = {
    "sdxl-draft": ("en",),
    "sdxl-creative-refine": ("en",),
    "qwen-image": ("zh", "en"),
    "wan2.2-ti2v-5b": ("zh", "en"),
}


class AIDirectorPlugin:
    """Route image and video requests through the appropriate Director method."""

    name: str = "ai_director"
    enabled: bool = True
    depends_on: list[str] = ["prompt_cache"]

    def __init__(
        self,
        strategy: AIDirectorStrategy,
        config: GenerationOptimizationConfig,
    ) -> None:
        self._strategy = strategy
        self._config = config

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        if not self._config.ai_director.enabled:
            return ctx

        options = self._generation_options(ctx)
        if options.get("prompt_mode") == "raw":
            ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})["ai_director"] = {
                "applicable": False,
                "reason": "raw_prompt_requested",
                "duration_ms": 0.0,
            }
            return ctx

        start_time = time.monotonic()
        prompt = self._extract_prompt(ctx)
        reference_images = self._extract_reference_images(ctx)
        modality = "mllm" if reference_images else "llm"

        try:
            if ctx.pipeline_kind == "generation:video":
                result = await self._execute_video(
                    ctx,
                    prompt=prompt,
                    reference_images=reference_images,
                    options=options,
                    modality=modality,
                    started_at=start_time,
                )
            else:
                result = await self._execute_image(
                    ctx,
                    prompt=prompt,
                    reference_images=reference_images,
                    options=options,
                    modality=modality,
                    started_at=start_time,
                )
            ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})[
                "ai_director"
            ] = result
            ctx.add_plugin_trace(
                "ai_director",
                float(result["duration_ms"]),
                "success",
                payload={
                    "modality": modality,
                    "video_plan": "video_plan" in result,
                    "has_reference_images": bool(reference_images),
                    "reference_image_count": len(reference_images),
                    "source_language": result.get("source_language"),
                    "output_language": result.get("output_language"),
                },
            )
        except Exception as exc:
            duration_ms = (time.monotonic() - start_time) * 1000.0
            ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})[
                "ai_director"
            ] = {
                "optimized_prompt": prompt,
                "original_prompt": prompt,
                "template_used": None,
                "model_used": None,
                "modality": modality,
                "cost_usd": 0.0,
                "duration_ms": duration_ms,
                "has_reference_images": bool(reference_images),
                "reference_image_count": len(reference_images),
                "error": str(exc),
            }
        return ctx

    async def _execute_image(
        self,
        ctx: PipelineContext,
        *,
        prompt: str,
        reference_images: list[MediaContent],
        options: dict[str, Any],
        modality: str,
        started_at: float,
    ) -> dict[str, Any]:
        target_languages = self._image_target_languages(options)
        result = await self._strategy.optimize_prompt(
            prompt=prompt,
            reference_images=reference_images,
            config=self._config.ai_director,
            ctx=ctx,
            target_languages=target_languages,
            source_language=self._optional_language(options.get("language")),
        )
        return {
            "optimized_prompt": result.optimized_prompt,
            "original_prompt": result.original_prompt,
            "template_used": result.template_used,
            "model_used": result.model_used,
            "modality": modality,
            "cost_usd": result.cost_usd,
            "duration_ms": (time.monotonic() - started_at) * 1000.0,
            "has_reference_images": bool(reference_images),
            "reference_image_count": len(reference_images),
            "source_language": result.source_language,
            "output_language": result.output_language,
            "target_languages": list(target_languages),
            "language_fallback_reason": result.language_fallback_reason,
        }

    async def _execute_video(
        self,
        ctx: PipelineContext,
        *,
        prompt: str,
        reference_images: list[MediaContent],
        options: dict[str, Any],
        modality: str,
        started_at: float,
    ) -> dict[str, Any]:
        timing = self._config.draft_workflow
        duration_seconds = self._number_option(
            options,
            "duration_seconds",
            ctx.request.get(
                "duration_seconds",
                timing.video_default_duration_seconds,
            ),
        )
        fps = self._integer_option(
            options,
            "fps",
            ctx.request.get("target_fps", timing.video_default_fps),
        )
        plan = await self._strategy.build_video_generation_plan(
            prompt=prompt,
            reference_images=reference_images,
            config=self._config.ai_director,
            ctx=ctx,
            duration_seconds=duration_seconds,
            fps=fps,
            source_draft_id=self._optional_text(
                options.get("source_draft_id")
                or ctx.request.get("source_draft_id")
            ),
            source_image_sha256=self._optional_text(
                options.get("source_image_sha256")
                or ctx.request.get("source_image_sha256")
            ),
            source_language=self._optional_language(options.get("language")),
            # The current keyframe stage uses SDXL. Wan itself supports zh/en.
            keyframe_languages=self._language_list(
                options.get("keyframe_languages"), default=("en",)
            ),
            motion_languages=self._language_list(
                options.get("motion_languages"), default=("zh", "en")
            ),
        )
        serialized_plan = asdict(plan)
        return {
            # Compatibility for the existing DraftGeneratorPlugin: image draft
            # generation receives the static keyframe prompt.
            "optimized_prompt": plan.keyframe_prompt,
            "original_prompt": plan.source_prompt,
            "template_used": None,
            "model_used": plan.model_used,
            "modality": modality,
            "cost_usd": plan.cost_usd,
            "duration_ms": (time.monotonic() - started_at) * 1000.0,
            "has_reference_images": bool(reference_images),
            "reference_image_count": len(reference_images),
            "source_language": plan.prompt_language,
            "output_language": plan.keyframe_language,
            "language_fallback_reason": plan.language_fallback_reason,
            "video_plan": serialized_plan,
        }

    @staticmethod
    def _generation_options(ctx: PipelineContext) -> dict[str, Any]:
        value = ctx.request.get("generation_options", {})
        return value if isinstance(value, dict) else {}

    @classmethod
    def _image_target_languages(
        cls,
        options: dict[str, Any],
    ) -> tuple[str, ...]:
        explicit = options.get("target_languages") or options.get("languages")
        if explicit:
            return cls._language_list(explicit, default=("en",))
        preset_id = str(options.get("preset_id") or "")
        if preset_id.startswith("checkpoint."):
            return ("en",)
        return _PRESET_LANGUAGES.get(preset_id, ("en",))

    @staticmethod
    def _language_list(
        value: Any,
        *,
        default: tuple[str, ...],
    ) -> tuple[str, ...]:
        if isinstance(value, str):
            candidates = (value,)
        elif isinstance(value, (list, tuple)):
            candidates = tuple(str(item) for item in value)
        else:
            candidates = default
        result: list[str] = []
        for item in candidates:
            code = item.strip().lower().replace("_", "-").split("-", 1)[0]
            if code and code not in result:
                result.append(code)
        return tuple(result or default)

    @staticmethod
    def _number_option(
        options: dict[str, Any],
        key: str,
        fallback: Any,
    ) -> float:
        value = options.get(key, fallback)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be numeric")
        return float(value)

    @staticmethod
    def _integer_option(
        options: dict[str, Any],
        key: str,
        fallback: Any,
    ) -> int:
        value = options.get(key, fallback)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
        return value

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @classmethod
    def _optional_language(cls, value: Any) -> str | None:
        text = cls._optional_text(value)
        return text.lower() if text else None

    def _extract_prompt(self, ctx: PipelineContext) -> str:
        messages = ctx.request.get("messages", [])
        if not isinstance(messages, list):
            return ""
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(
                    str(part.get("text", ""))
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            return ""
        return str(ctx.request.get("prompt") or "")

    def _extract_reference_images(self, ctx: PipelineContext) -> list[MediaContent]:
        references: list[MediaContent] = []
        media_opt = ctx.extra.get("media_optimization", {})
        per_media = (
            media_opt.get("per_media_results", [])
            if isinstance(media_opt, dict)
            else []
        )
        for result in per_media:
            if isinstance(result, MediaContent):
                if result.media_type == MediaType.IMAGE:
                    references.append(result)
            elif isinstance(result, dict) and result.get("media_type") in {
                "image",
                MediaType.IMAGE,
            }:
                references.append(
                    MediaContent(
                        media_type=MediaType.IMAGE,
                        source_url=result.get("source_url"),
                        mime_type=result.get("mime_type"),
                        size_bytes=int(result.get("size_bytes", 0) or 0),
                        metadata=result.get("metadata", {}),
                    )
                )
        if references:
            return references

        messages = ctx.request.get("messages", [])
        if not isinstance(messages, list):
            return references
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content", [])
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict) or part.get("type") != "image_url":
                        continue
                    image_url = part.get("image_url", {})
                    url = (
                        image_url.get("url", "")
                        if isinstance(image_url, dict)
                        else image_url
                    )
                    if isinstance(url, str) and url:
                        references.append(
                            MediaContent(
                                media_type=MediaType.IMAGE,
                                source_url=url,
                            )
                        )
            break
        return references
