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
    assert request.source_draft_id is None
    assert request.keyframe_prompt is None
    assert request.motion_prompt is None
    assert request.prompt_language is None


def test_video_generation_plan_serializes_as_draft_snapshot():
    plan = VideoGenerationPlan(
        source_prompt="生成一只柯基摇尾巴向镜头跑来，5秒",
        keyframe_prompt="一只黄白色柯基站在草地中央，正面看向镜头",
        motion_prompt="柯基摇动尾巴并向镜头跑来，保持主体和场景一致",
        prompt_language="zh",
        duration_seconds=5.0,
        fps=8,
        frame_count=40,
        source_draft_id="draft-image-1",
        source_image_sha256="abc123",
    )

    assert asdict(plan) == {
        "source_prompt": "生成一只柯基摇尾巴向镜头跑来，5秒",
        "keyframe_prompt": "一只黄白色柯基站在草地中央，正面看向镜头",
        "motion_prompt": "柯基摇动尾巴并向镜头跑来，保持主体和场景一致",
        "prompt_language": "zh",
        "duration_seconds": 5.0,
        "fps": 8,
        "frame_count": 40,
        "source_draft_id": "draft-image-1",
        "source_image_sha256": "abc123",
        "fallback_reason": None,
    }


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("keyframe_prompt", ""),
        ("motion_prompt", "  "),
        ("prompt_language", ""),
        ("duration_seconds", 0),
        ("fps", 0),
        ("frame_count", 0),
    ],
)
def test_video_generation_plan_rejects_invalid_boundaries(field_name, value):
    values = {
        "source_prompt": "a corgi runs toward the camera",
        "keyframe_prompt": "a corgi standing on grass",
        "motion_prompt": "the corgi runs toward the camera",
        "prompt_language": "en",
        "duration_seconds": 5.0,
        "fps": 8,
        "frame_count": 40,
    }
    values[field_name] = value

    with pytest.raises(ValueError):
        VideoGenerationPlan(**values)
