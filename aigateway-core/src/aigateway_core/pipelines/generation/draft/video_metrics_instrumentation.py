"""Attach progressive-video metrics to the public draft strategy.

The project already uses small installation modules for cross-cutting runtime
behavior. Keeping observability here avoids duplicating or replacing the current
request-recovery and cancellation implementation in ``draft_generator.py``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from aigateway_core.pipelines.generation._common.metrics import (
    get_prometheus_registry,
)

logger = logging.getLogger(__name__)
_MARKER = "_aigateway_video_metrics_installed"


def _record(action: str, *args: Any, **kwargs: Any) -> None:
    """Keep metrics best-effort so observability cannot break generation."""
    try:
        method = getattr(get_prometheus_registry(), action)
        method(*args, **kwargs)
    except Exception as exc:
        logger.debug(
            "progressive video metric update failed: action=%s error=%s",
            action,
            type(exc).__name__,
        )


def install_video_metrics_instrumentation() -> None:
    """Instrument keyframe and Wan lifecycle methods exactly once."""
    from .draft_generator import DraftGeneratorStrategy

    strategy = DraftGeneratorStrategy
    if getattr(strategy, _MARKER, False):
        return

    original_freeze = strategy._freeze_video_keyframe
    original_validate = strategy._validate_frozen_video_keyframe
    original_generate_video = strategy._generate_video_with_comfyui

    @classmethod
    def freeze_with_metrics(cls: type[Any], draft: Any) -> None:
        params = getattr(draft, "generation_params", {})
        before_hash = str(params.get("source_image_sha256") or "")
        before_marker = str(params.get("source_image_frozen_draft_id") or "")

        original_freeze(draft)

        params = getattr(draft, "generation_params", {})
        after_hash = str(params.get("source_image_sha256") or "")
        after_marker = str(params.get("source_image_frozen_draft_id") or "")
        newly_frozen = bool(after_hash) and after_marker == draft.draft_id and (
            not before_hash or before_marker not in {"", draft.draft_id}
        )
        if not newly_frozen:
            return
        source_kind = str(params.get("source_kind") or cls._source_kind(draft))
        _record("inc_video_reference_source", source_kind)
        _record("inc_video_keyframe", "success")

    @staticmethod
    def validate_with_metrics(draft: Any) -> None:
        try:
            original_validate(draft)
        except Exception as exc:
            if "video_keyframe_integrity_mismatch" in str(exc):
                _record("inc_video_keyframe_integrity_mismatch")
            raise

    async def generate_video_with_metrics(self: Any, draft: Any) -> bytes:
        started_at = time.monotonic()
        try:
            result = await original_generate_video(self, draft)
        except BaseException:
            _record("inc_video_generation", "failure")
            raise

        elapsed = max(0.0, time.monotonic() - started_at)
        params = getattr(draft, "generation_params", {})
        duration = params.get("duration_seconds")
        frame_count = params.get("frame_count")
        _record("inc_video_generation", "success")
        if isinstance(frame_count, int) and not isinstance(frame_count, bool):
            _record(
                "observe_video_generation_duration",
                elapsed,
                duration_bucket=str(duration or "unknown"),
                frame_count=frame_count,
            )
        return result

    strategy._freeze_video_keyframe = freeze_with_metrics
    strategy._validate_frozen_video_keyframe = validate_with_metrics
    strategy._generate_video_with_comfyui = generate_video_with_metrics
    setattr(strategy, _MARKER, True)


__all__ = ["install_video_metrics_instrumentation"]
