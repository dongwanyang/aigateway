from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.pipelines.generation._common.config import (
    GenerationOptimizationConfig,
)
from aigateway_core.pipelines.generation.draft.draft_generator_plugin import (
    DraftGeneratorPlugin,
)


def _plugin() -> DraftGeneratorPlugin:
    return DraftGeneratorPlugin(
        strategy=MagicMock(),
        config=GenerationOptimizationConfig(),
    )


def _video_context(duration_seconds: object, fps: object = 8) -> PipelineContext:
    return PipelineContext(
        request={
            "messages": [
                {"role": "user", "content": "生成一段视频"},
            ],
            "generation_options": {
                "duration_seconds": duration_seconds,
                "fps": fps,
            },
        },
        trace_id="trace-video-duration-contract",
        pipeline_kind="generation:video",
    )


@pytest.mark.parametrize(
    ("duration_seconds", "expected_frames"),
    [(3, 25), (5, 41), (8, 65)],
)
def test_generation_options_control_video_frame_count(
    duration_seconds: int,
    expected_frames: int,
) -> None:
    request = _plugin()._build_generation_request(
        _video_context(duration_seconds),
    )

    assert request.duration_seconds == float(duration_seconds)
    assert request.target_fps == 8
    assert request.frame_count == expected_frames


@pytest.mark.parametrize("duration_seconds", [0, 4, 9, True, "5"])
def test_invalid_video_duration_options_fail_closed(duration_seconds: object) -> None:
    with pytest.raises(ValueError):
        _plugin()._build_generation_request(
            _video_context(duration_seconds),
        )


def test_invalid_video_fps_fails_closed() -> None:
    with pytest.raises(ValueError):
        _plugin()._build_generation_request(
            _video_context(5, fps=0),
        )
