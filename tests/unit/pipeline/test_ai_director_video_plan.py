from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.pipelines.generation._common.config import AIDirectorConfig
from aigateway_core.pipelines.generation.director.ai_director import (
    _EXPAND_SYSTEM_PROMPT,
    _REWRITE_SYSTEM_PROMPT,
    AIDirectorStrategy,
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


def test_default_prompts_preserve_user_language():
    for system_prompt in (_REWRITE_SYSTEM_PROMPT, _EXPAND_SYSTEM_PROMPT):
        assert "输出语言必须与用户提示词的主要语言一致" in system_prompt
        assert "只有调用方明确说明目标模型不支持用户语言时" in system_prompt


@pytest.mark.asyncio
async def test_builds_chinese_video_plan(config, ctx):
    bridge = AsyncMock()
    bridge.completion = AsyncMock(
        return_value={
            "data": {
                "choices": [
                    {
                        "message": {
                            "content": """```json
{
  "keyframe_prompt": "一只黄白色柯基站在草地中央，正面看向镜头",
  "motion_prompt": "柯基摇动尾巴并向镜头跑来",
  "duration_seconds": 5,
  "language": "zh"
}
```"""
                        }
                    }
                ]
            }
        }
    )
    strategy = AIDirectorStrategy(config=config, litellm_bridge=bridge)

    plan = await strategy.build_video_generation_plan(
        "生成一只柯基摇尾巴向镜头跑来，5秒",
        [],
        config,
        ctx,
        duration_seconds=5,
        fps=8,
    )

    assert plan.prompt_language == "zh"
    assert "站在草地中央" in plan.keyframe_prompt
    assert "向镜头跑来" in plan.motion_prompt
    assert "保持主体身份" in plan.motion_prompt
    assert plan.duration_seconds == 5
    assert plan.fps == 8
    assert plan.frame_count == 40
    assert plan.fallback_reason is None


@pytest.mark.asyncio
async def test_builds_english_video_plan(config, ctx):
    bridge = AsyncMock()
    bridge.completion = AsyncMock(
        return_value={
            "data": {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"keyframe_prompt":"A corgi stands on grass facing the camera",'
                                '"motion_prompt":"The corgi wags its tail and runs forward",'
                                '"duration_seconds":5,"language":"en"}'
                            )
                        }
                    }
                ]
            }
        }
    )
    strategy = AIDirectorStrategy(config=config, litellm_bridge=bridge)

    plan = await strategy.build_video_generation_plan(
        "A corgi wags its tail and runs toward the camera",
        [],
        config,
        ctx,
    )

    assert plan.prompt_language == "en"
    assert "Keep the subject identity" in plan.motion_prompt
    assert plan.frame_count == 40


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected_reason"),
    [
        ("not-json", "invalid_json"),
        ("", "empty_response"),
        ('{"keyframe_prompt":"frame only"}', "missing_motion_prompt"),
    ],
)
async def test_invalid_video_plan_falls_back(config, ctx, content, expected_reason):
    bridge = AsyncMock()
    bridge.completion = AsyncMock(
        return_value={"data": {"choices": [{"message": {"content": content}}]}}
    )
    strategy = AIDirectorStrategy(config=config, litellm_bridge=bridge)

    plan = await strategy.build_video_generation_plan(
        "一只柯基向镜头跑来",
        [],
        config,
        ctx,
    )

    assert plan.keyframe_prompt == "一只柯基向镜头跑来"
    assert plan.prompt_language == "zh"
    assert plan.fallback_reason == expected_reason


@pytest.mark.asyncio
async def test_video_plan_timeout_is_traceable(config, ctx):
    config.timeout_seconds = 0.01

    async def slow_completion(**kwargs):
        await asyncio.sleep(1)
        return {}

    bridge = AsyncMock()
    bridge.completion = slow_completion
    strategy = AIDirectorStrategy(config=config, litellm_bridge=bridge)

    plan = await strategy.build_video_generation_plan(
        "A corgi runs toward the camera",
        [],
        config,
        ctx,
    )

    assert plan.prompt_language == "en"
    assert plan.fallback_reason == "timeout"


@pytest.mark.asyncio
async def test_video_plan_without_bridge_uses_safe_fallback(config, ctx):
    strategy = AIDirectorStrategy(config=config, litellm_bridge=None)

    plan = await strategy.build_video_generation_plan(
        "一只柯基向镜头跑来",
        [],
        config,
        ctx,
    )

    assert plan.fallback_reason == "no_bridge"
    assert "保持主体身份" in plan.motion_prompt
