from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.pipelines.generation._common.config import (
    GenerationOptimizationConfig,
)
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
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
        # Director reports the requested sample count. The draft boundary
        # normalizes it to the Wan-compatible 4n+1 length (41).
        "frame_count": 40,
        "source_draft_id": "draft-source",
        "source_image_sha256": "client-claimed-hash",
        "fallback_reason": None,
        "language_fallback_reason": "keyframe:target_model_language_unsupported",
        "model_used": "gpt-4o-mini",
        "cost_usd": 0.001,
    }


def _video_ctx(plan: dict | None = None) -> PipelineContext:
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
    if plan is not None:
        ctx.extra[NS_GENERATION_OPTIMIZATION] = {
            "ai_director": {
                "optimized_prompt": plan["keyframe_prompt"],
                "video_plan": plan,
            }
        }
    return ctx


def test_draft_plugin_maps_and_normalizes_video_plan():
    strategy = MagicMock()
    plugin = DraftGeneratorPlugin(
        strategy=strategy,
        config=GenerationOptimizationConfig(),
    )
    plan = _video_plan()
    ctx = _video_ctx(plan)

    request = plugin._build_generation_request(ctx)

    assert request.prompt == plan["keyframe_prompt"]
    assert request.source_prompt == plan["source_prompt"]
    assert request.keyframe_prompt == plan["keyframe_prompt"]
    assert request.motion_prompt == plan["motion_prompt"]
    assert request.prompt_language == "zh"
    assert request.keyframe_language == "en"
    assert request.motion_language == "zh"
    assert request.duration_seconds == 5.0
    assert request.target_fps == 8
    assert request.frame_count == 41
    assert plan["frame_count"] == 41
    assert request.source_draft_id == "draft-source"
    assert request.source_image_sha256 == "client-claimed-hash"


@pytest.mark.parametrize(
    ("duration", "expected_frames"),
    [(3.0, 25), (5.0, 41), (8.0, 65)],
)
def test_draft_plugin_normalizes_supported_wan_durations(
    duration,
    expected_frames,
):
    strategy = MagicMock()
    plugin = DraftGeneratorPlugin(
        strategy=strategy,
        config=GenerationOptimizationConfig(),
    )
    plan = _video_plan()
    plan["duration_seconds"] = duration
    plan["frame_count"] = round(duration * 8)

    request = plugin._build_generation_request(_video_ctx(plan))

    assert request.frame_count == expected_frames


def test_draft_plugin_rejects_inconsistent_frame_count():
    strategy = MagicMock()
    plugin = DraftGeneratorPlugin(
        strategy=strategy,
        config=GenerationOptimizationConfig(),
    )
    plan = _video_plan()
    plan["frame_count"] = 42

    with pytest.raises(ValueError, match="video_plan_frame_count_mismatch"):
        plugin._build_generation_request(_video_ctx(plan))


def test_draft_plugin_rejects_unsafe_translated_fallback():
    strategy = MagicMock()
    plugin = DraftGeneratorPlugin(
        strategy=strategy,
        config=GenerationOptimizationConfig(),
    )
    plan = _video_plan()
    plan["fallback_reason"] = "invalid_json"
    ctx = _video_ctx(plan)
    request = plugin._build_generation_request(ctx)

    with pytest.raises(DraftWorkflowError, match="video_prompt_plan_unavailable"):
        plugin._assert_video_plan_ready(ctx, request)


def test_pending_video_draft_freezes_keyframe_before_storage():
    strategy = object.__new__(DraftGeneratorStrategy)
    preview = b"persisted-preview"
    draft = DraftResult(
        draft_id="draft-freeze",
        previews=[preview],
        generation_params={"has_reference_image": False},
        created_at=0,
        expires_at=100,
        status="pending",
        media_type="video",
    )

    strategy._freeze_video_keyframe(draft)

    assert draft.generation_params["source_image_sha256"] == (
        hashlib.sha256(preview).hexdigest()
    )
    assert draft.generation_params["source_kind"] == "generated_keyframe"


@pytest.mark.asyncio
async def test_wan_confirmation_consumes_frozen_motion_plan_and_timing():
    strategy = object.__new__(DraftGeneratorStrategy)
    strategy._comfyui_config = SimpleNamespace(
        video_enabled=True,
        video_execution_timeout=30,
    )
    strategy._comfyui_semaphore = asyncio.Semaphore(1)
    strategy._ensure_storage_capacity = AsyncMock(return_value=None)
    strategy._upload_image = AsyncMock(return_value="keyframe.png")
    workflow = {"5": {"inputs": {}}, "11": {"inputs": {}}}
    strategy._build_video_workflow = MagicMock(return_value=workflow)
    strategy._comfy_client_id = MagicMock(return_value="client-id")
    strategy._submit_workflow = AsyncMock(return_value="prompt-id")
    strategy._record_comfy_job = AsyncMock(return_value=None)
    strategy._poll_result = AsyncMock(return_value=b"video")

    preview = b"png"
    draft = DraftResult(
        draft_id="draft-video",
        previews=[preview],
        generation_params={
            "prompt": "static keyframe prompt",
            "motion_prompt": "motion-only prompt",
            "seed": 7,
            "trace_id": "trace-video",
            "frame_count": 41,
            "fps": 8,
            "source_image_sha256": hashlib.sha256(preview).hexdigest(),
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
    assert workflow["5"]["inputs"]["length"] == 41
    assert workflow["11"]["inputs"]["fps"] == 8.0


@pytest.mark.asyncio
async def test_wan_confirmation_rejects_changed_keyframe_bytes():
    strategy = object.__new__(DraftGeneratorStrategy)
    strategy._comfyui_config = SimpleNamespace(video_enabled=True)
    draft = DraftResult(
        draft_id="draft-integrity",
        previews=[b"changed"],
        generation_params={
            "source_image_sha256": hashlib.sha256(b"original").hexdigest(),
            "frame_count": 41,
            "fps": 8,
        },
        created_at=0,
        expires_at=100,
        media_type="video",
    )

    with pytest.raises(
        DraftWorkflowError,
        match="video_keyframe_integrity_mismatch",
    ):
        await strategy._generate_video_with_comfyui(draft)
