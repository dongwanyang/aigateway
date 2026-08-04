from __future__ import annotations

import asyncio
import math
from unittest.mock import AsyncMock

import pytest

from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.pipelines.generation._common.config import AIDirectorConfig
from aigateway_core.pipelines.generation.director.ai_director import (
    _EXPAND_SYSTEM_PROMPT,
    _REWRITE_SYSTEM_PROMPT,
    AIDirectorStrategy,
    _detect_prompt_language,
    _ensure_consistency_constraint,
)


@pytest.fixture
def config():
    return AIDirectorConfig(
        enabled=True,
        rewrite_model="gpt-4o-mini",
        timeout_seconds=1.0,
        max_prompt_length=2000,
        min_prompt_length=10,
    )


@pytest.fixture
def ctx():
    return PipelineContext(
        request={"model": "test", "messages": []},
        request_id="req-video-plan",
        trace_id="trace-video-plan",
    )


def _bridge(content: str):
    bridge = AsyncMock()
    bridge.completion = AsyncMock(
        return_value={
            "data": {"choices": [{"message": {"content": content}}]},
            "_meta": {"cost": 0.001},
        }
    )
    return bridge


def test_default_prompts_use_caller_model_language_policy():
    for system_prompt in (_REWRITE_SYSTEM_PROMPT, _EXPAND_SYSTEM_PROMPT):
        assert "目标模型语言策略" in system_prompt
        assert "无论用户使用何种语言" not in system_prompt
        assert "引号内" not in system_prompt or "原样保留" in system_prompt


@pytest.mark.asyncio
async def test_chinese_video_uses_english_sdxl_keyframe_and_chinese_wan_motion(
    config, ctx
):
    bridge = _bridge(
        """{
          "keyframe_prompt": "A yellow and white corgi stands still in the center of a grassy field, facing the camera",
          "motion_prompt": "柯基摇动尾巴并向镜头跑来",
          "language": "zh"
        }"""
    )
    plan = await AIDirectorStrategy(
        config=config, litellm_bridge=bridge
    ).build_video_generation_plan(
        "生成一只柯基摇尾巴向镜头跑来，5秒",
        [],
        config,
        ctx,
        duration_seconds=5,
        fps=8,
        keyframe_languages=("en",),
        motion_languages=("zh", "en"),
    )

    assert plan.prompt_language == "zh"
    assert plan.keyframe_language == "en"
    assert plan.motion_language == "zh"
    assert "grassy field" in plan.keyframe_prompt
    assert "向镜头跑来" in plan.motion_prompt
    assert "脸部" in plan.motion_prompt
    assert "不切换场景" in plan.motion_prompt
    assert plan.frame_count == 40
    assert plan.fallback_reason is None


@pytest.mark.asyncio
async def test_english_video_plan(config, ctx):
    bridge = _bridge(
        """{
          "keyframe_prompt": "A corgi stands still on grass facing the camera",
          "motion_prompt": "The corgi wags its tail and runs forward",
          "language": "en"
        }"""
    )
    plan = await AIDirectorStrategy(
        config=config, litellm_bridge=bridge
    ).build_video_generation_plan(
        "A corgi wags its tail and runs toward the camera",
        [],
        config,
        ctx,
    )

    assert plan.keyframe_language == "en"
    assert plan.motion_language == "en"
    assert "Keep the subject identity" in plan.motion_prompt
    assert plan.frame_count == 40


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected_reason"),
    [
        ("not-json", "invalid_json"),
        ("", "empty_response"),
        ('{"keyframe_prompt":"frame only"}', "missing_motion_prompt"),
        (
            '{"keyframe_prompt":"一只柯基站在草地上","motion_prompt":"柯基向前跑"}',
            "keyframe_language_mismatch",
        ),
        (
            '{"keyframe_prompt":"A corgi runs forward","motion_prompt":"柯基向前跑"}',
            "keyframe_contains_motion",
        ),
    ],
)
async def test_invalid_video_plan_falls_back_without_motion_in_keyframe(
    config, ctx, content, expected_reason
):
    plan = await AIDirectorStrategy(
        config=config, litellm_bridge=_bridge(content)
    ).build_video_generation_plan(
        "一只黄白色柯基在草地上，摇尾巴并向镜头跑来",
        [],
        config,
        ctx,
    )

    assert plan.keyframe_language == "en"
    assert plan.motion_language == "zh"
    assert "向镜头跑来" not in plan.keyframe_prompt
    assert "摇尾巴" not in plan.keyframe_prompt
    assert "static pose" in plan.keyframe_prompt
    assert plan.fallback_reason == expected_reason


@pytest.mark.asyncio
async def test_video_plan_timeout_is_traceable(config, ctx):
    config.timeout_seconds = 0.01

    async def slow_completion(**kwargs):
        await asyncio.sleep(1)
        return {}

    bridge = AsyncMock()
    bridge.completion = slow_completion
    plan = await AIDirectorStrategy(
        config=config, litellm_bridge=bridge
    ).build_video_generation_plan(
        "A corgi runs toward the camera",
        [],
        config,
        ctx,
    )

    assert plan.fallback_reason == "timeout"
    assert "runs toward" not in plan.keyframe_prompt.lower()


@pytest.mark.asyncio
async def test_no_bridge_fallback_records_language_conversion(config, ctx):
    plan = await AIDirectorStrategy(
        config=config, litellm_bridge=None
    ).build_video_generation_plan(
        "一只柯基向镜头跑来",
        [],
        config,
        ctx,
    )

    assert plan.fallback_reason == "no_bridge"
    assert plan.language_fallback_reason == (
        "keyframe:target_model_language_unsupported"
    )
    assert plan.keyframe_language == "en"
    assert plan.motion_language == "zh"


def test_partial_consistency_text_is_completed():
    result = _ensure_consistency_constraint("柯基向前跑，保持主体一致。", "zh")
    assert "脸部" in result
    assert "颜色" in result
    assert "身体比例" in result
    assert "场景一致" in result
    assert "不切换场景" in result


def test_language_detection_ignores_quoted_on_screen_text():
    assert _detect_prompt_language('Create a poster containing the text "你好"') == "en"
    assert _detect_prompt_language("生成海报，文字为“Hello”") == "zh"
    assert _detect_prompt_language("猫がゆっくり歩く") == "ja"


@pytest.mark.asyncio
async def test_image_prompt_language_policy_is_model_aware(config, ctx):
    bridge = _bridge(
        "Subject: A corgi on grass\nAction: Standing still\n"
        "Environment: Daylight\nCamera: Medium shot"
    )
    strategy = AIDirectorStrategy(config=config, litellm_bridge=bridge)
    result = await strategy.optimize_prompt(
        "一只柯基站在草地上",
        [],
        config,
        ctx,
        target_languages=("en",),
    )

    assert result.source_language == "zh"
    assert result.output_language == "en"
    assert result.language_fallback_reason == "target_model_language_unsupported"
    system_prompt = bridge.completion.call_args.kwargs["messages"][0]["content"]
    assert "目标模型支持的输出语言已选择为 en" in system_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("duration", "fps", "message"),
    [
        (float("nan"), 8, "duration_seconds must be finite"),
        (float("inf"), 8, "duration_seconds must be finite"),
        (4, 8, "video_duration_unsupported"),
        (5, 8.5, "fps must be an integer"),
        (5, True, "fps must be an integer"),
        (5, 61, "fps_out_of_range"),
    ],
)
async def test_invalid_video_timing_is_rejected_before_fallback(
    config, ctx, duration, fps, message
):
    strategy = AIDirectorStrategy(config=config, litellm_bridge=None)
    with pytest.raises(ValueError, match=message):
        await strategy.build_video_generation_plan(
            "A corgi runs",
            [],
            config,
            ctx,
            duration_seconds=duration,
            fps=fps,
        )
