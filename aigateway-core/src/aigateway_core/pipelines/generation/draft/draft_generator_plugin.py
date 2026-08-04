"""DraftGeneratorPlugin — progressive generation workflow adapter."""

from __future__ import annotations

import math
import time
from typing import Any

from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.pipelines.generation._common.config import (
    GenerationOptimizationConfig,
)
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation._common.models import (
    DraftResult,
    GenerationRequest,
)
from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)
from aigateway_core.prefix.media.types import MediaContent, MediaType

NS_GENERATION_OPTIMIZATION = "generation_optimization"
_SUPPORTED_VIDEO_DURATIONS = (3.0, 5.0, 8.0)
_MAX_VIDEO_FPS = 60
_MAX_VIDEO_FRAMES = 481


class DraftGeneratorPlugin:
    """Build GenerationRequest objects and submit progressive drafts."""

    name: str = "draft_generator"
    enabled: bool = True
    depends_on: list[str] = ["token_compressor"]

    def __init__(
        self,
        strategy: DraftGeneratorStrategy,
        config: GenerationOptimizationConfig,
    ) -> None:
        self._strategy = strategy
        self._config = config

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        if not self._config.draft_workflow.enabled:
            return ctx

        options = self._generation_options(ctx)
        if options.get("backend") == "cloud":
            faithful_4k = options.get("quality") == "faithful_4k"
            ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})[
                "draft_generator"
            ] = {
                "applicable": False,
                "reason": (
                    "invalid_generation_options"
                    if faithful_4k
                    else "cloud_backend_requested"
                ),
                "local_error": (
                    "faithful_4k requires the local ComfyUI backend"
                    if faithful_4k
                    else None
                ),
                "duration_ms": 0.0,
            }
            return ctx

        started_at = time.monotonic()
        try:
            if not self._is_generation_request(ctx):
                ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})[
                    "draft_generator"
                ] = {
                    "applicable": False,
                    "reason": "not_a_generation_request",
                    "duration_ms": (time.monotonic() - started_at) * 1000.0,
                }
                return ctx

            request = self._build_generation_request(ctx)
            self._assert_video_plan_ready(ctx, request)
            backend = str(options.get("backend") or "auto")
            try:
                await self._strategy.check_local_dependencies(request)
            except DraftWorkflowError as exc:
                ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})[
                    "draft_generator"
                ] = {
                    "applicable": False,
                    "reason": (
                        "local_backend_unavailable"
                        if backend == "local" or request.quality == "faithful_4k"
                        else "auto_fallback_to_cloud"
                    ),
                    "local_error": str(exc),
                    "duration_ms": (time.monotonic() - started_at) * 1000.0,
                }
                return ctx

            owner_user_id = (
                ctx.extra["draft_owner_user_id"]
                if "draft_owner_user_id" in ctx.extra
                else (ctx.extra.get("user_id") or ctx.user_id)
            )
            owner_group_id = (
                ctx.extra["draft_owner_group_id"]
                if "draft_owner_group_id" in ctx.extra
                else ctx.extra.get("group_id")
            )
            draft_result: DraftResult = await self._strategy.generate_draft(
                request=request,
                config=self._config.draft_workflow,
                keyframe_count=self._extract_keyframe_count(ctx),
                chat_session_id=ctx.extra.get("chat_session_id"),
                user_id=owner_user_id,
                group_id=owner_group_id,
            )
            duration_ms = (time.monotonic() - started_at) * 1000.0
            actual_checkpoint = draft_result.generation_params.get(
                "checkpoint",
                self._strategy.checkpoint_name,
            )
            ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})[
                "draft_generator"
            ] = {
                "applicable": True,
                "draft_id": draft_result.draft_id,
                "preview_count": len(draft_result.previews),
                "attempt_number": draft_result.attempt_number,
                "max_attempts": draft_result.max_attempts,
                "expires_at": draft_result.expires_at,
                "status": draft_result.status,
                "generation_params": draft_result.generation_params,
                "draft_model": f"comfyui:{actual_checkpoint}",
                "duration_ms": duration_ms,
            }
            ctx.add_plugin_trace(
                "draft_generator",
                duration_ms,
                "success",
                payload={
                    "applicable": True,
                    "preview_count": len(draft_result.previews),
                    "status": draft_result.status,
                    "has_video_plan": bool(request.motion_prompt),
                    "frame_count": request.frame_count,
                },
            )
            ctx.should_stop = True
        except ValueError as exc:
            duration_ms = (time.monotonic() - started_at) * 1000.0
            ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})[
                "draft_generator"
            ] = {
                "applicable": False,
                "reason": "invalid_generation_options",
                "local_error": str(exc),
                "duration_ms": duration_ms,
            }
            ctx.add_plugin_trace(
                "draft_generator",
                duration_ms,
                "failed",
                payload={"reason": "invalid_generation_options"},
            )
            ctx.should_stop = True
        except Exception as exc:
            duration_ms = (time.monotonic() - started_at) * 1000.0
            ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})[
                "draft_generator"
            ] = {
                "applicable": True,
                "draft_id": None,
                "preview_count": 0,
                "duration_ms": duration_ms,
                "error": str(exc),
            }
        return ctx

    def _assert_video_plan_ready(
        self,
        ctx: PipelineContext,
        request: GenerationRequest,
    ) -> None:
        if request.media_type != "video":
            return
        generation = ctx.extra.get(NS_GENERATION_OPTIMIZATION, {})
        director = (
            generation.get("ai_director", {})
            if isinstance(generation, dict)
            else {}
        )
        if not isinstance(director, dict):
            return
        if director.get("error") and not request.motion_prompt:
            raise DraftWorkflowError("video_prompt_plan_unavailable")
        plan = director.get("video_plan")
        if not isinstance(plan, dict):
            return
        fallback_reason = self._optional_text(plan.get("fallback_reason"))
        prompt_language = self._optional_text(plan.get("prompt_language"))
        keyframe_language = self._optional_text(plan.get("keyframe_language"))
        motion_language = self._optional_text(plan.get("motion_language"))
        requires_conversion = bool(
            prompt_language
            and (
                (keyframe_language and keyframe_language != prompt_language)
                or (motion_language and motion_language != prompt_language)
            )
        )
        if fallback_reason and requires_conversion:
            raise DraftWorkflowError("video_prompt_plan_unavailable")

    def _is_generation_request(self, ctx: PipelineContext) -> bool:
        if ctx.request.get("draft_workflow") or ctx.request.get("enable_draft"):
            return True
        if ctx.request.get("generation_mode"):
            return True
        if ctx.pipeline_kind in ("generation:image", "generation:video"):
            return True
        model = ctx.request.get("model", "")
        if isinstance(model, str):
            lowered = model.lower()
            return any(
                keyword in lowered
                for keyword in (
                    "image",
                    "video",
                    "generative",
                    "dall-e",
                    "stable-diffusion",
                )
            )
        return False

    def _build_generation_request(self, ctx: PipelineContext) -> GenerationRequest:
        options = self._generation_options(ctx)
        video_plan = self._video_plan(ctx)
        media_type = "video" if ctx.pipeline_kind == "generation:video" else "image"

        target_resolution = self._target_resolution(ctx, options)
        quality = str(options.get("quality") or "standard")
        preset_id = self._optional_text(options.get("preset_id"))
        required_vram = options.get("required_vram_gb")
        source_prompt = self._extract_original_prompt(ctx)

        if media_type == "video" and video_plan:
            keyframe_prompt = self._required_plan_text(
                video_plan, "keyframe_prompt"
            )
            motion_prompt = self._required_plan_text(
                video_plan, "motion_prompt"
            )
            duration_seconds, target_fps, normalized_count = (
                self._normalize_video_timing(
                    video_plan.get("duration_seconds"),
                    video_plan.get("fps"),
                )
            )
            supplied_count = self._positive_integer(
                video_plan.get("frame_count"), "frame_count"
            )
            raw_count = round(duration_seconds * target_fps)
            if supplied_count not in {raw_count, normalized_count}:
                raise ValueError("video_plan_frame_count_mismatch")
            frame_count = normalized_count
            # Keep the shared plan and persisted request snapshot consistent.
            video_plan["frame_count"] = frame_count
            prompt_language = self._required_plan_text(
                video_plan, "prompt_language"
            )
            keyframe_language = self._required_plan_text(
                video_plan, "keyframe_language"
            )
            motion_language = self._required_plan_text(
                video_plan, "motion_language"
            )
            language_fallback_reason = self._optional_text(
                video_plan.get("language_fallback_reason")
            )
            source_draft_id = self._optional_text(
                video_plan.get("source_draft_id")
            )
            source_image_sha256 = self._optional_text(
                video_plan.get("source_image_sha256")
            )
            prompt = keyframe_prompt
        else:
            prompt = self._extract_prompt(ctx)
            keyframe_prompt = None
            motion_prompt = None
            if media_type == "video":
                duration_seconds, target_fps, frame_count = (
                    self._normalize_video_timing(
                        options.get(
                            "duration_seconds",
                            ctx.request.get("duration_seconds", 5.0),
                        ),
                        options.get("fps", ctx.request.get("target_fps", 8)),
                    )
                )
            else:
                duration_seconds = self._finite_positive_number(
                    options.get(
                        "duration_seconds",
                        ctx.request.get("duration_seconds", 5.0),
                    ),
                    "duration_seconds",
                )
                target_fps = self._positive_integer(
                    options.get("fps", ctx.request.get("target_fps", 8)),
                    "fps",
                )
                frame_count = None
            prompt_language = None
            keyframe_language = None
            motion_language = None
            language_fallback_reason = None
            source_draft_id = self._optional_text(
                options.get("source_draft_id")
                or ctx.request.get("source_draft_id")
            )
            source_image_sha256 = self._optional_text(
                options.get("source_image_sha256")
                or ctx.request.get("source_image_sha256")
            )

        return GenerationRequest(
            prompt=prompt,
            source_prompt=source_prompt,
            reference_images=self._extract_reference_images(ctx),
            target_resolution=target_resolution,
            target_fps=target_fps,
            media_type=media_type,
            duration_seconds=duration_seconds,
            frame_count=frame_count,
            source_draft_id=source_draft_id,
            source_image_sha256=source_image_sha256,
            keyframe_prompt=keyframe_prompt,
            motion_prompt=motion_prompt,
            prompt_language=prompt_language,
            keyframe_language=keyframe_language,
            motion_language=motion_language,
            language_fallback_reason=language_fallback_reason,
            quality=quality,
            preset_id=preset_id,
            required_vram_gb=(
                float(required_vram) if required_vram is not None else None
            ),
            api_key_id=self._api_key_id(ctx),
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
        )

    @classmethod
    def _normalize_video_timing(
        cls,
        duration_value: Any,
        fps_value: Any,
    ) -> tuple[float, int, int]:
        duration = cls._finite_positive_number(
            duration_value, "duration_seconds"
        )
        if not any(
            math.isclose(duration, allowed)
            for allowed in _SUPPORTED_VIDEO_DURATIONS
        ):
            raise ValueError("video_duration_unsupported")
        fps = cls._positive_integer(fps_value, "fps")
        if fps > _MAX_VIDEO_FPS:
            raise ValueError("fps_out_of_range")
        requested_count = round(duration * fps)
        normalized_count = ((requested_count - 1 + 3) // 4) * 4 + 1
        if normalized_count <= 0 or normalized_count > _MAX_VIDEO_FRAMES:
            raise ValueError("frame_count_out_of_range")
        return duration, fps, normalized_count

    @staticmethod
    def _generation_options(ctx: PipelineContext) -> dict[str, Any]:
        options = ctx.request.get("generation_options", {})
        return options if isinstance(options, dict) else {}

    @staticmethod
    def _video_plan(ctx: PipelineContext) -> dict[str, Any] | None:
        generation = ctx.extra.get(NS_GENERATION_OPTIMIZATION, {})
        if not isinstance(generation, dict):
            return None
        director = generation.get("ai_director", {})
        if not isinstance(director, dict):
            return None
        plan = director.get("video_plan")
        return plan if isinstance(plan, dict) else None

    @staticmethod
    def _target_resolution(
        ctx: PipelineContext,
        options: dict[str, Any],
    ) -> tuple[int, int]:
        if options.get("width") and options.get("height"):
            result: Any = (int(options["width"]), int(options["height"]))
        else:
            result = ctx.request.get("target_resolution", (1920, 1080))
        if isinstance(result, list):
            result = tuple(result)
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in result
            )
        ):
            raise ValueError("invalid_target_resolution")
        return result

    @staticmethod
    def _api_key_id(ctx: PipelineContext) -> str:
        value = ctx.request.get("api_key_id", "")
        if not value and ctx.user_id:
            value = ctx.user_id
        return str(value or "")

    @staticmethod
    def _required_plan_text(plan: dict[str, Any], key: str) -> str:
        value = plan.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"invalid_video_plan:{key}")
        return value.strip()

    @staticmethod
    def _finite_positive_number(value: Any, name: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
        ):
            raise ValueError(f"{name} must be a finite positive number")
        return float(value)

    @staticmethod
    def _positive_integer(value: Any, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return value

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _extract_reference_images(ctx: PipelineContext) -> list[MediaContent]:
        media_opt = ctx.extra.get("media_optimization", {})
        results = (
            media_opt.get("per_media_results", [])
            if isinstance(media_opt, dict)
            else []
        )
        references: list[MediaContent] = []
        for result in results:
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
                        raw_data=result.get("raw_data"),
                        optimized_data=result.get("optimized_data"),
                        mime_type=result.get("mime_type"),
                        size_bytes=int(result.get("size_bytes", 0) or 0),
                    )
                )
        if references:
            return references

        urls = ctx.request.get("reference_image_urls", [])
        if isinstance(urls, list):
            for url in urls:
                if isinstance(url, str) and url:
                    references.append(
                        MediaContent(
                            media_type=MediaType.IMAGE,
                            source_url=url,
                            mime_type=(
                                url[5:].split(";", 1)[0]
                                if url.startswith("data:image/")
                                else None
                            ),
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
                                mime_type=(
                                    url[5:].split(";", 1)[0]
                                    if url.startswith("data:image/")
                                    else None
                                ),
                            )
                        )
            break
        return references

    @staticmethod
    def _extract_original_prompt(ctx: PipelineContext) -> str:
        messages = ctx.request.get("messages", [])
        if isinstance(messages, list):
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
        return str(ctx.request.get("prompt") or "")

    def _extract_prompt(self, ctx: PipelineContext) -> str:
        generation = ctx.extra.get(NS_GENERATION_OPTIMIZATION, {})
        if isinstance(generation, dict):
            director = generation.get("ai_director", {})
            if isinstance(director, dict):
                value = director.get("optimized_prompt")
                if isinstance(value, str) and value:
                    return value
        return self._extract_original_prompt(ctx)

    @staticmethod
    def _extract_keyframe_count(ctx: PipelineContext) -> int | None:
        value = ctx.request.get("keyframe_count")
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
