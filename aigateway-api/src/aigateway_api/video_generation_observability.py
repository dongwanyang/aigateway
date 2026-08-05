"""Structured, privacy-preserving observability for Wan video submissions."""
from __future__ import annotations

import functools
import hashlib
import logging
from collections.abc import Mapping
from typing import Any

from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)

from .runtime_identity import deployed_commit_sha

logger = logging.getLogger(__name__)
_ORIGINAL_ATTR = "_aigateway_original_record_comfy_job"
_WRAPPER_ATTR = "_aigateway_video_observability_wrapper"


def _text_hash(value: Any) -> str:
    text = str(value or "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def video_submission_fields(
    draft: Any,
    *,
    prompt_id: str,
    input_image_name: str | None = None,
) -> dict[str, Any]:
    """Build log fields without exposing prompt text or image bytes."""
    raw_params = getattr(draft, "generation_params", {})
    params: Mapping[str, Any] = raw_params if isinstance(raw_params, Mapping) else {}
    draft_id = str(getattr(draft, "draft_id", "") or "")
    return {
        "request_id": str(params.get("request_id") or ""),
        "trace_id": str(params.get("trace_id") or ""),
        "draft_id": draft_id,
        "source_draft_id": str(params.get("source_draft_id") or ""),
        "source_image_sha256": str(params.get("source_image_sha256") or ""),
        "source_kind": str(params.get("source_kind") or ""),
        "prompt_language": str(params.get("prompt_language") or ""),
        "keyframe_prompt_hash": _text_hash(params.get("keyframe_prompt")),
        "motion_prompt_hash": _text_hash(params.get("motion_prompt")),
        "duration_seconds": params.get("duration_seconds"),
        "fps": params.get("fps"),
        "frame_count": params.get("frame_count"),
        "workflow_version": str(
            params.get("video_workflow_version")
            or params.get("workflow_version")
            or getattr(draft, "workflow_version", "")
            or ""
        ),
        "input_image_name": input_image_name or f"video-keyframe-{draft_id}.png",
        "comfyui_prompt_id": prompt_id,
        "deployed_commit_sha": deployed_commit_sha(),
    }


def install_video_generation_observability() -> None:
    """Log each persisted video ComfyUI prompt exactly once per submission."""
    current = DraftGeneratorStrategy._record_comfy_job
    if getattr(current, _WRAPPER_ATTR, False):
        return
    if not hasattr(DraftGeneratorStrategy, _ORIGINAL_ATTR):
        setattr(DraftGeneratorStrategy, _ORIGINAL_ATTR, current)
    original = getattr(DraftGeneratorStrategy, _ORIGINAL_ATTR)

    @functools.wraps(original)
    async def record_with_video_log(
        self: Any,
        draft_id: str,
        prompt_id: str,
        stage: str,
    ) -> Any:
        result = await original(self, draft_id, prompt_id, stage)
        if stage != "refining":
            return result
        try:
            draft = await self.get_draft(draft_id)
            if draft is None or getattr(draft, "media_type", None) != "video":
                return result
            logger.info(
                "video_generation.workflow_submitted",
                extra=video_submission_fields(draft, prompt_id=prompt_id),
            )
        except Exception as exc:
            logger.warning(
                "video_generation.workflow_log_failed",
                extra={
                    "draft_id": draft_id,
                    "comfyui_prompt_id": prompt_id,
                    "error_type": type(exc).__name__,
                },
            )
        return result

    setattr(record_with_video_log, _WRAPPER_ATTR, True)
    DraftGeneratorStrategy._record_comfy_job = record_with_video_log


__all__ = ["install_video_generation_observability", "video_submission_fields"]
