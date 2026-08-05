"""Structured, privacy-preserving fields for Wan video submissions.

The progressive-video plan (section 9.2) requires that a single log line can
explain "the subject changed / the length is wrong / the frame is static": which
image was used, in which language the prompts were written, how many frames were
requested, and which ComfyUI job ran it.

Prompt text and image bytes are never logged; only lengths, hashes and ids.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from typing import Any

_COMMIT_SHA_ENV_VARS = (
    "AIGATEWAY_COMMIT_SHA",
    "GIT_COMMIT_SHA",
    "SOURCE_VERSION",
    "RENDER_GIT_COMMIT",
)


def text_hash(value: Any) -> str:
    """Hash prompt text so drift is detectable without storing the prompt."""
    text = str(value or "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def deployed_commit_sha() -> str:
    """Report the running build, so code review and behavior can be reconciled."""
    for name in _COMMIT_SHA_ENV_VARS:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


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
        "keyframe_prompt_hash": text_hash(params.get("keyframe_prompt")),
        "motion_prompt_hash": text_hash(params.get("motion_prompt")),
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


__all__ = ["deployed_commit_sha", "text_hash", "video_submission_fields"]
