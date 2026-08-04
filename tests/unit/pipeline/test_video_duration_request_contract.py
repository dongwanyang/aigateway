from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.pipelines.generation._common.config import (
    GenerationOptimizationConfig,
)
from aigateway_core.pipelines.generation.draft.draft_generator_plugin import (
    DraftGeneratorPlugin,
)


def _plugin(
    config: GenerationOptimizationConfig | None = None,
) -> tuple[DraftGeneratorPlugin, MagicMock]:
    strategy = MagicMock()
    strategy.check_local_dependencies = AsyncMock()
    strategy.generate_draft = AsyncMock()
    return (
        DraftGeneratorPlugin(
            strategy=strategy,
            config=config or GenerationOptimizationConfig(),
        ),
        strategy,
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
    plugin, _strategy = _plugin()

    request = plugin._build_generation_request(
        _video_context(duration_seconds),
    )

    assert request.duration_seconds == float(duration_seconds)
    assert request.target_fps == 8
    assert request.frame_count == expected_frames


def test_configured_duration_allowlist_controls_normalization() -> None:
    config = GenerationOptimizationConfig()
    config.draft_workflow.video_supported_durations_seconds = (3, 5)
    plugin, _strategy = _plugin(config)

    with pytest.raises(ValueError, match="video_duration_unsupported"):
        plugin._build_generation_request(_video_context(8))


def test_configured_frame_limit_controls_normalization() -> None:
    config = GenerationOptimizationConfig()
    config.draft_workflow.video_max_frames = 40
    plugin, _strategy = _plugin(config)

    with pytest.raises(ValueError, match="frame_count_out_of_range"):
        plugin._build_generation_request(_video_context(5))


@pytest.mark.asyncio
@pytest.mark.parametrize("duration_seconds", [0, 4, 9, True, "5"])
async def test_invalid_video_duration_options_fail_closed_in_execute(
    duration_seconds: object,
) -> None:
    plugin, strategy = _plugin()

    result = await plugin.execute(_video_context(duration_seconds))

    draft_info = result.extra["generation_optimization"]["draft_generator"]
    assert result.should_stop is True
    assert draft_info["applicable"] is False
    assert draft_info["reason"] == "invalid_generation_options"
    assert draft_info["local_error"]
    strategy.check_local_dependencies.assert_not_awaited()
    strategy.generate_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_video_fps_fails_closed_in_execute() -> None:
    plugin, strategy = _plugin()

    result = await plugin.execute(_video_context(5, fps=0))

    draft_info = result.extra["generation_optimization"]["draft_generator"]
    assert result.should_stop is True
    assert draft_info["applicable"] is False
    assert draft_info["reason"] == "invalid_generation_options"
    assert draft_info["local_error"] == "fps must be a positive integer"
    strategy.check_local_dependencies.assert_not_awaited()
    strategy.generate_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_strategy_value_error_is_not_misreported_as_invalid_options() -> None:
    plugin, strategy = _plugin()
    strategy.generate_draft.side_effect = ValueError("draft storage failed")

    result = await plugin.execute(_video_context(5))

    draft_info = result.extra["generation_optimization"]["draft_generator"]
    assert result.should_stop is False
    assert draft_info["applicable"] is True
    assert "reason" not in draft_info
    assert draft_info["error"] == "draft storage failed"
