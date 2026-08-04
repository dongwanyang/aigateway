"""Create a frozen video draft from an existing completed image draft."""
from __future__ import annotations

import hashlib
import math
import re
import secrets
import time
import uuid
from typing import Any

from aigateway_core.pipelines.generation._common.exceptions import (
    DraftWorkflowError,
)
from aigateway_core.pipelines.generation._common.models import (
    DRAFT_STATUS_COMPLETED,
    DRAFT_STATUS_CONFIRMED,
    DRAFT_STATUS_PENDING,
    DraftResult,
    GenerationRequest,
)
from aigateway_core.prefix.media.types import MediaContent, MediaType

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def _prompt_language(text: str) -> str:
    return "zh" if _CJK_RE.search(text) else "en"


def _motion_prompt_with_consistency(prompt: str, language: str) -> str:
    """Add the same identity/scene guard used by the normal video planner."""
    if language == "zh":
        constraint = "保持主体身份、脸部、颜色、身体比例和场景一致，不切换场景。"
    else:
        constraint = (
            "Keep the subject identity, face, colors, body proportions, and "
            "scene consistent; do not switch scenes."
        )
    if constraint.lower() in prompt.lower():
        return prompt
    separator = "" if prompt.endswith(("。", ".", "!", "！", "?", "？")) else (
        "。" if language == "zh" else "."
    )
    return f"{prompt}{separator} {constraint}".strip()


def _normalize_frames(
    config: Any,
    duration_seconds: float,
    fps: int,
) -> tuple[float, int, int]:
    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, (int, float))
        or not math.isfinite(float(duration_seconds))
    ):
        raise DraftWorkflowError("video_duration_unsupported")
    duration = float(duration_seconds)
    supported = tuple(
        float(value) for value in config.video_supported_durations_seconds
    )
    if not any(math.isclose(duration, value) for value in supported):
        raise DraftWorkflowError("video_duration_unsupported")
    if (
        isinstance(fps, bool)
        or not isinstance(fps, int)
        or fps <= 0
        or fps > config.video_max_fps
    ):
        raise DraftWorkflowError("video_duration_unsupported")

    requested = round(duration * fps)
    requested = max(config.video_min_frames, requested)
    requested = min(config.video_max_frames, requested)
    frame_count = ((requested - 1 + 3) // 4) * 4 + 1
    if frame_count > config.video_max_frames:
        raise DraftWorkflowError("video_duration_unsupported")
    return duration, fps, frame_count


def _owner_matches(
    source: DraftResult,
    *,
    user_id: str | None,
    group_id: str | None,
) -> bool:
    if source.user_id and source.user_id != user_id:
        return False
    if source.group_id and source.group_id != group_id:
        return False
    if not source.user_id and not source.group_id:
        return False
    return True


async def create_video_draft_from_source(
    strategy: Any,
    *,
    source_draft_id: str,
    motion_prompt: str,
    duration_seconds: float,
    fps: int,
    chat_session_id: str,
    user_id: str | None,
    group_id: str | None,
    trace_id: str | None = None,
) -> DraftResult:
    """Copy a completed image result into a new immutable video draft."""
    source_prompt = str(motion_prompt or "").strip()
    if not source_prompt:
        raise DraftWorkflowError("video_motion_prompt_missing")
    session_id = str(chat_session_id or "").strip()
    if not session_id:
        raise DraftWorkflowError("source_draft_forbidden")

    try:
        source = await strategy.get_draft(source_draft_id)
    except (TypeError, ValueError) as exc:
        raise DraftWorkflowError("source_draft_not_found") from exc
    if source is None:
        raise DraftWorkflowError("source_draft_not_found")
    if not _owner_matches(source, user_id=user_id, group_id=group_id):
        raise DraftWorkflowError("source_draft_forbidden")
    if source.session_id != session_id:
        raise DraftWorkflowError("source_draft_forbidden")
    if source.media_type != "image" or source.status not in {
        DRAFT_STATUS_COMPLETED,
        DRAFT_STATUS_CONFIRMED,
    }:
        raise DraftWorkflowError("source_draft_invalid_type")

    try:
        source_bytes = await strategy.get_result_bytes(source_draft_id)
    except DraftWorkflowError as exc:
        raise DraftWorkflowError("source_draft_not_found") from exc
    if not source_bytes:
        raise DraftWorkflowError("source_draft_not_found")

    config = strategy._config
    duration, normalized_fps, frame_count = _normalize_frames(
        config,
        duration_seconds,
        fps,
    )
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    source_params = (
        source.generation_params
        if isinstance(source.generation_params, dict)
        else {}
    )
    keyframe_prompt = str(
        source_params.get("keyframe_prompt")
        or source_params.get("prompt")
        or "Use the frozen source image as the exact video keyframe."
    ).strip()
    language = _prompt_language(source_prompt)
    planned_motion_prompt = _motion_prompt_with_consistency(
        source_prompt,
        language,
    )
    keyframe_language = str(
        source_params.get("keyframe_language") or language
    )
    request_id = uuid.uuid4().hex
    resolved_trace_id = trace_id or request_id

    dependency_request = GenerationRequest(
        prompt=keyframe_prompt,
        source_prompt=source_prompt,
        reference_images=[
            MediaContent(
                media_type=MediaType.IMAGE,
                raw_data=source_bytes,
                mime_type="image/png",
                size_bytes=len(source_bytes),
            )
        ],
        media_type="video",
        duration_seconds=duration,
        target_fps=normalized_fps,
        frame_count=frame_count,
        source_draft_id=source_draft_id,
        source_image_sha256=source_hash,
        keyframe_prompt=keyframe_prompt,
        motion_prompt=planned_motion_prompt,
        prompt_language=language,
        keyframe_language=keyframe_language,
        motion_language=language,
        target_resolution=config.default_target_resolution,
        preset_id="wan2.2-ti2v-5b",
        request_id=request_id,
        trace_id=resolved_trace_id,
    )
    await strategy.check_local_dependencies(dependency_request)

    now = time.time()
    draft_id = uuid.uuid4().hex
    comfy_config = strategy._comfyui_config
    workflow_version = str(
        getattr(comfy_config, "workflow_version", "") or ""
    )
    video_workflow_version = str(
        getattr(comfy_config, "video_workflow_version", "")
        or workflow_version
    )
    required_vram = float(
        getattr(comfy_config, "video_required_vram_gb", 0.0) or 0.0
    )
    params = {
        "prompt": keyframe_prompt,
        "source_prompt": source_prompt,
        "keyframe_prompt": keyframe_prompt,
        "motion_prompt": planned_motion_prompt,
        "prompt_language": language,
        "keyframe_language": keyframe_language,
        "motion_language": language,
        "language_fallback_reason": None,
        "duration_seconds": duration,
        "fps": normalized_fps,
        "frame_count": frame_count,
        "source_draft_id": source_draft_id,
        "source_kind": "draft_result",
        "source_image_sha256": source_hash,
        "source_image_frozen_draft_id": draft_id,
        "source_result_sha256": source_hash,
        "has_reference_image": True,
        "target_resolution": list(config.default_target_resolution),
        "media_type": "video",
        "quality": "standard",
        "preset_id": "wan2.2-ti2v-5b",
        "checkpoint": "source-draft",
        "seed": secrets.randbelow(2**31),
        "request_id": request_id,
        "trace_id": resolved_trace_id,
        "required_vram_gb": required_vram,
        "required_vram_explicit": False,
        "workflow_version": workflow_version,
        "video_workflow_version": video_workflow_version,
    }
    ttl_seconds = config.retention_period_hours * 3600
    draft = DraftResult(
        draft_id=draft_id,
        previews=[source_bytes],
        generation_params=params,
        created_at=now,
        expires_at=now + ttl_seconds,
        attempt_number=1,
        max_attempts=config.max_regeneration_attempts,
        status=DRAFT_STATUS_PENDING,
        media_type="video",
        session_id=session_id,
        user_id=user_id,
        group_id=group_id,
        progress=1.0,
        stage="preview_ready",
        workflow_version=video_workflow_version,
    )
    await strategy._store_draft(draft, ttl_seconds)
    return draft
