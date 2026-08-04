from __future__ import annotations

from dataclasses import asdict

import pytest

from aigateway_core.pipelines.generation._common.models import (
    GenerationRequest,
    VideoGenerationPlan,
)


def test_generation_request_exposes_video_plan_fields():
    request = GenerationRequest(prompt="一只柯基向镜头跑来", media_type="video")

    assert request.duration_seconds == 5.0
    assert request.target_fps == 8
    assert request.frame_count is None
    assert request.source_draft_id is None
    assert request.source_image_sha256 is None
    assert request.keyframe_prompt is None
    assert request.motion_prompt is None
    assert request.prompt_language is None
    assert request.keyframe_language is None
    assert request.motion_language is None


def test_video_generation_plan_serializes_as_draft_snapshot():
    plan = VideoGenerationPlan(
        source_prompt="生成一只柯基摇尾巴向镜头跑来，5秒",
        keyframe_prompt="A yellow and white corgi stands still on grass",
        motion_prompt="柯基摇动尾巴并向镜头跑来，保持主体和场景一致",
        prompt_language="zh",
        keyframe_language="en",
        motion_language="zh",
        duration_seconds=5.0,
        fps=8,
        frame_count=40,
        source_draft_id="draft-image-1",
        source_image_sha256="abc123",
        language_fallback_reason="keyframe:target_model_language_unsupported",
    )

    assert asdict(plan) == {
        "source_prompt": "生成一只柯基摇尾巴向镜头跑来，5秒",
        "keyframe_prompt": "A yellow and white corgi stands still on grass",
        "motion_prompt": "柯基摇动尾巴并向镜头跑来，保持主体和场景一致",
        "prompt_language": "zh",
        "keyframe_language": "en",
        "motion_language": "zh",
        "duration_seconds": 5.0,
        "fps": 8,
        "frame_count": 40,
        "source_draft_id": "draft-image-1",
        "source_image_sha256": "abc123",
        "fallback_reason": None,
        "language_fallback_reason": "keyframe:target_model_language_unsupported",
        "model_used": None,
        "cost_usd": 0.0,
    }


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("keyframe_prompt", ""),
        ("motion_prompt", "  "),
        ("prompt_language", ""),
        ("keyframe_language", ""),
        ("motion_language", ""),
        ("duration_seconds", 0),
        ("duration_seconds", float("nan")),
        ("fps", 0),
        ("fps", 8.5),
        ("frame_count", 0),
        ("frame_count", 40.5),
    ],
)
def test_video_generation_plan_rejects_invalid_boundaries(field_name, value):
    values = {
        "source_prompt": "a corgi runs toward the camera",
        "keyframe_prompt": "a corgi standing on grass",
        "motion_prompt": "the corgi runs toward the camera",
        "prompt_language": "en",
        "keyframe_language": "en",
        "motion_language": "en",
        "duration_seconds": 5.0,
        "fps": 8,
        "frame_count": 40,
    }
    values[field_name] = value

    with pytest.raises(ValueError):
        VideoGenerationPlan(**values)
