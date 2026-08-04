from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.pipelines.generation._common.config import (
    DraftWorkflowConfig,
    GenerationOptimizationConfig,
)
from aigateway_core.pipelines.generation._common.models import DraftResult
from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)
from aigateway_core.pipelines.generation.draft.draft_generator_plugin import (
    NS_GENERATION_OPTIMIZATION,
    DraftGeneratorPlugin,
)


def _video_plan() -> dict:
    return {
        "source_prompt": "生成一只柯基向镜头跑来",
        "keyframe_prompt": (
            "A yellow-and-white corgi stands on grass, facing the camera "
            "in a static opening pose."
        ),
        "motion_prompt": (
            "柯基向镜头跑来。保持主体身份、脸部、颜色、身体比例和场景一致，"
            "不切换场景。"
        ),
        "prompt_language": "zh",
        "keyframe_language": "en",
        "motion_language": "zh",
        "duration_seconds": 5.0,
        "fps": 8,
        "frame_count": 40,
        "source_draft_id": "draft-source",
        "source_image_sha256": "abc123",
        "fallback_reason": None,
        "language_fallback_reason": "keyframe:target_model_language_unsupported",
        "model_used": "gpt-4o-mini",
        "cost_usd": 0.001,
    }


def test_draft_plugin_maps_video_plan_into_generation_request():
    strategy = MagicMock()
    config = GenerationOptimizationConfig()
    plugin = DraftGeneratorPlugin(strategy=strategy, config=config)
    ctx = PipelineContext(
        request={
            "messages": [
                {"role": "user", "content": "生成一只柯基向镜头跑来"}
            ],
            "generation_options": {"preset_id": "wan2.2-ti2v-5b"},
        },
        trace_id="trace-draft-plan",
        pipeline_kind="generation:video",
    )
    ctx.extra[NS_GENERATION_OPTIMIZATION] = {
        "ai_director": {
            "optimized_prompt": _video_plan()["keyframe_prompt"],
            "video_plan": _video_plan(),
        }
    }

    request = plugin._build_generation_request(ctx)

    assert request.prompt == _video_plan()["keyframe_prompt"]
    assert request.source_prompt == _video_plan()["source_prompt"]
    assert request.keyframe_prompt == _video_plan()["keyframe_prompt"]
    assert request.motion_prompt == _video_plan()["motion_prompt"]
    assert request.prompt_language == "zh"
    assert request.keyframe_language == "en"
    assert request.motion_language == "zh"
    assert request.duration_seconds == 5.0
    assert request.target_fps == 8
    assert request.frame_count == 40
    assert request.source_draft_id == "draft-source"
    assert request.source_image_sha256 == "abc123"


def test_draft_plugin_rejects_inconsistent_frame_count():
    strategy = MagicMock()
    plugin = DraftGeneratorPlugin(
        strategy=strategy,
        config=GenerationOptimizationConfig(),
    )
    plan = _video_plan()
    plan["frame_count"] = 41
    ctx = PipelineContext(
        request={"messages": [{"role": "user", "content": "video"}]},
        trace_id="trace-bad-plan",
        pipeline_kind="generation:video",
    )
    ctx.extra[NS_GENERATION_OPTIMIZATION] = {
        "ai_director": {"video_plan": plan}
    }

    with pytest.raises(ValueError, match="video_plan_frame_count_mismatch"):
        plugin._build_generation_request(ctx)


@pytest.mark.asyncio
async def test_wan_confirmation_consumes_motion_prompt_not_keyframe_prompt():
    strategy = object.__new__(DraftGeneratorStrategy)
    strategy._comfyui_config = SimpleNamespace(
        video_enabled=True,
        video_execution_timeout=30,
    )
    strategy._comfyui_semaphore = asyncio.Semaphore(1)
    strategy._ensure_storage_capacity = AsyncMock(return_value=None)
    strategy._upload_image = AsyncMock(return_value="keyframe.png")
    strategy._build_video_workflow = MagicMock(return_value={"workflow": True})
    strategy._comfy_client_id = MagicMock(return_value="client-id")
    strategy._submit_workflow = AsyncMock(return_value="prompt-id")
    strategy._record_comfy_job = AsyncMock(return_value=None)
    strategy._poll_result = AsyncMock(return_value=b"video")

    draft = DraftResult(
        draft_id="draft-video",
        previews=[b"png"],
        generation_params={
            "prompt": "static keyframe prompt",
            "motion_prompt": "motion-only prompt",
            "seed": 7,
            "trace_id": "trace-video",
        },
        created_at=0,
        expires_at=100,
        media_type="video",
    )

    result = await strategy._generate_video_with_comfyui(draft)

    assert result == b"video"
    strategy._build_video_workflow.assert_called_once_with(
        input_name="keyframe.png",
        prompt="motion-only prompt",
        seed=7,
        draft_id="draft-video",
    )
