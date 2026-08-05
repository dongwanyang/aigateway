from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.pipelines.generation._common.config import (
    GenerationOptimizationConfig,
)
from aigateway_core.pipelines.generation._common.models import VideoGenerationPlan
from aigateway_core.pipelines.generation.director.ai_director_plugin import (
    AIDirectorPlugin,
)


@pytest.mark.asyncio
async def test_ai_director_uses_configured_video_timing_defaults() -> None:
    config = GenerationOptimizationConfig()
    config.draft_workflow.video_default_duration_seconds = 3
    config.draft_workflow.video_default_fps = 12

    strategy = MagicMock()
    strategy.build_video_generation_plan = AsyncMock(
        return_value=VideoGenerationPlan(
            source_prompt="生成一段视频",
            keyframe_prompt="A static opening frame.",
            motion_prompt="主体向前移动。",
            prompt_language="zh",
            keyframe_language="en",
            motion_language="zh",
            duration_seconds=3.0,
            fps=12,
            frame_count=36,
        ),
    )
    plugin = AIDirectorPlugin(strategy=strategy, config=config)
    ctx = PipelineContext(
        request={
            "messages": [{"role": "user", "content": "生成一段视频"}],
            "generation_options": {},
        },
        trace_id="trace-director-timing-defaults",
        pipeline_kind="generation:video",
    )

    result = await plugin.execute(ctx)

    strategy.build_video_generation_plan.assert_awaited_once()
    call = strategy.build_video_generation_plan.await_args.kwargs
    assert call["duration_seconds"] == 3.0
    assert call["fps"] == 12
    plan = result.extra["generation_optimization"]["ai_director"]["video_plan"]
    assert plan["duration_seconds"] == 3.0
    assert plan["fps"] == 12
