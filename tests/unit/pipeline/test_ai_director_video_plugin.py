from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.pipelines.generation._common.config import (
    AIDirectorConfig,
    GenerationOptimizationConfig,
)
from aigateway_core.pipelines.generation._common.models import (
    PromptOptimizationResult,
    VideoGenerationPlan,
)
from aigateway_core.pipelines.generation.director.ai_director import (
    AIDirectorStrategy,
)
from aigateway_core.pipelines.generation.director.ai_director_plugin import (
    NS_GENERATION_OPTIMIZATION,
    AIDirectorPlugin,
)


@pytest.fixture
def config() -> GenerationOptimizationConfig:
    value = GenerationOptimizationConfig()
    value.ai_director = AIDirectorConfig(
        enabled=True,
        rewrite_model="gpt-4o-mini",
        timeout_seconds=10,
        max_prompt_length=2000,
        min_prompt_length=10,
    )
    return value


@pytest.mark.asyncio
async def test_video_pipeline_builds_and_publishes_structured_plan(config):
    strategy = MagicMock(spec=AIDirectorStrategy)
    strategy.build_video_generation_plan = AsyncMock(
        return_value=VideoGenerationPlan(
            source_prompt="生成一只柯基向镜头跑来",
            keyframe_prompt=(
                "A yellow-and-white corgi stands on grass, facing the camera "
                "in a static opening pose."
            ),
            motion_prompt=(
                "柯基向镜头跑来。保持主体身份、脸部、颜色、身体比例和场景一致，"
                "不切换场景。"
            ),
            prompt_language="zh",
            keyframe_language="en",
            motion_language="zh",
            duration_seconds=5.0,
            fps=8,
            frame_count=40,
            language_fallback_reason="keyframe:target_model_language_unsupported",
            model_used="gpt-4o-mini",
            cost_usd=0.001,
        )
    )
    strategy.optimize_prompt = AsyncMock()
    plugin = AIDirectorPlugin(strategy=strategy, config=config)
    ctx = PipelineContext(
        request={
            "messages": [
                {"role": "user", "content": "生成一只柯基向镜头跑来"}
            ],
            "generation_options": {
                "preset_id": "wan2.2-ti2v-5b",
                "duration_seconds": 5,
                "fps": 8,
            },
        },
        trace_id="trace-video",
        pipeline_kind="generation:video",
    )

    result = await plugin.execute(ctx)

    assert result is ctx
    strategy.optimize_prompt.assert_not_called()
    strategy.build_video_generation_plan.assert_awaited_once()
    call = strategy.build_video_generation_plan.call_args.kwargs
    assert call["keyframe_languages"] == ("en",)
    assert call["motion_languages"] == ("zh", "en")
    assert call["duration_seconds"] == 5.0
    assert call["fps"] == 8

    director = ctx.extra[NS_GENERATION_OPTIMIZATION]["ai_director"]
    assert director["optimized_prompt"].startswith("A yellow-and-white corgi")
    assert director["video_plan"]["keyframe_language"] == "en"
    assert director["video_plan"]["motion_language"] == "zh"
    assert director["video_plan"]["frame_count"] == 40


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("preset_id", "expected_languages"),
    [
        ("sdxl-draft", ("en",)),
        ("sdxl-creative-refine", ("en",)),
        ("qwen-image", ("zh", "en")),
        ("checkpoint.c2FmZQ", ("en",)),
    ],
)
async def test_image_pipeline_passes_target_model_languages(
    config,
    preset_id,
    expected_languages,
):
    strategy = MagicMock(spec=AIDirectorStrategy)
    strategy.optimize_prompt = AsyncMock(
        return_value=PromptOptimizationResult(
            optimized_prompt="optimized",
            original_prompt="原始提示",
            model_used="gpt-4o-mini",
            source_language="zh",
            output_language=expected_languages[0],
            language_fallback_reason=(
                None if "zh" in expected_languages else "target_model_language_unsupported"
            ),
        )
    )
    strategy.build_video_generation_plan = AsyncMock()
    plugin = AIDirectorPlugin(strategy=strategy, config=config)
    ctx = PipelineContext(
        request={
            "messages": [{"role": "user", "content": "原始提示"}],
            "generation_options": {"preset_id": preset_id},
        },
        trace_id="trace-image",
        pipeline_kind="generation:image",
    )

    await plugin.execute(ctx)

    call = strategy.optimize_prompt.call_args.kwargs
    assert call["target_languages"] == expected_languages
    strategy.build_video_generation_plan.assert_not_called()
