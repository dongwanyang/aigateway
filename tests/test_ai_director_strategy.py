"""
Tests for AIDirectorStrategy — AI 导演 Prompt 优化核心逻辑
============================================================

验证:
- 正常 prompt 改写: 调用 litellm_bridge 获取结构化输出
- 输出截断: 超过 max_prompt_length 时截断
- 超时处理: 超时后降级到原始 prompt
- 异常处理: 模型调用失败时降级到原始 prompt
- 短 prompt 扩展: 低于 min_prompt_length 时附加参考图信息
- 无 litellm_bridge: 直接返回原始 prompt
- apply_template: placeholder 返回空字符串

需求: 1.1, 1.2, 1.5, 1.6
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.pipelines.generation._common.config import AIDirectorConfig
from aigateway_core.pipelines.generation._common.models import PromptOptimizationResult
from aigateway_core.pipelines.generation.director.ai_director import (
    AIDirectorStrategy,
    _EXPAND_SYSTEM_PROMPT,
    _REWRITE_SYSTEM_PROMPT,
)
from aigateway_core.prefix.media.types import MediaContent, MediaType


@pytest.fixture
def default_config():
    """Default AI Director config."""
    return AIDirectorConfig(
        enabled=True,
        rewrite_model="gpt-4o-mini",
        timeout_seconds=10.0,
        max_prompt_length=2000,
        min_prompt_length=10,
        prompt_confirmation_enabled=True,
    )


@pytest.fixture
def pipeline_ctx():
    """Create a minimal PipelineContext for testing."""
    return PipelineContext(
        request={"model": "test-model", "messages": []},
        request_id="test-req-001",
        trace_id="test-trace-001",
    )


@pytest.fixture
def mock_bridge():
    """Create a mock LiteLLM bridge that returns a structured prompt."""
    bridge = AsyncMock()
    bridge.completion = AsyncMock(
        return_value={
            "data": {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "【主体】一位穿着红色连衣裙的年轻女性\n"
                                "【动作】在雨中旋转舞蹈\n"
                                "【环境】城市街道，霓虹灯闪烁，夜晚\n"
                                "【镜头】中景，低角度仰拍，缓慢环绕"
                            )
                        }
                    }
                ]
            },
            "_meta": {"cost": 0.00015},
        }
    )
    return bridge


class TestAIDirectorStrategyOptimizePrompt:
    """Tests for optimize_prompt method."""

    @pytest.mark.asyncio
    async def test_normal_rewrite(self, default_config, pipeline_ctx, mock_bridge):
        """Normal prompt rewrite should return structured output."""
        strategy = AIDirectorStrategy(config=default_config, litellm_bridge=mock_bridge)

        result = await strategy.optimize_prompt(
            prompt="一个女孩在雨中跳舞",
            reference_images=[],
            config=default_config,
            ctx=pipeline_ctx,
        )

        assert isinstance(result, PromptOptimizationResult)
        assert result.original_prompt == "一个女孩在雨中跳舞"
        assert "【主体】" in result.optimized_prompt
        assert "【动作】" in result.optimized_prompt
        assert "【环境】" in result.optimized_prompt
        assert "【镜头】" in result.optimized_prompt
        assert result.model_used == "gpt-4o-mini"
        assert result.cost_usd == 0.00015
        assert result.duration_ms > 0

    @pytest.mark.asyncio
    async def test_output_truncated_to_max_length(self, default_config, pipeline_ctx):
        """Output exceeding max_prompt_length should be truncated."""
        # Configure a very short max_prompt_length
        config = AIDirectorConfig(
            max_prompt_length=50,
            timeout_seconds=10.0,
            min_prompt_length=5,
        )

        long_response = "A" * 200
        bridge = AsyncMock()
        bridge.completion = AsyncMock(
            return_value={
                "data": {
                    "choices": [{"message": {"content": long_response}}]
                },
                "_meta": {"cost": 0.0001},
            }
        )

        strategy = AIDirectorStrategy(config=config, litellm_bridge=bridge)
        result = await strategy.optimize_prompt(
            prompt="test prompt here",
            reference_images=[],
            config=config,
            ctx=pipeline_ctx,
        )

        assert len(result.optimized_prompt) <= 50

    @pytest.mark.asyncio
    async def test_timeout_fallback_to_original(self, default_config, pipeline_ctx):
        """Timeout should result in fallback to original prompt."""
        config = AIDirectorConfig(
            timeout_seconds=0.01,  # Very short timeout
            min_prompt_length=5,
        )

        async def slow_completion(*args, **kwargs):
            await asyncio.sleep(1.0)  # Intentionally slow
            return {"data": {"choices": [{"message": {"content": "optimized"}}]}}

        bridge = AsyncMock()
        bridge.completion = slow_completion

        strategy = AIDirectorStrategy(config=config, litellm_bridge=bridge)
        result = await strategy.optimize_prompt(
            prompt="a dancing girl in rain",
            reference_images=[],
            config=config,
            ctx=pipeline_ctx,
        )

        assert result.optimized_prompt == "a dancing girl in rain"
        assert result.original_prompt == "a dancing girl in rain"

    @pytest.mark.asyncio
    async def test_exception_fallback_to_original(self, default_config, pipeline_ctx):
        """Any exception should result in fallback to original prompt."""
        bridge = AsyncMock()
        bridge.completion = AsyncMock(side_effect=RuntimeError("Model unavailable"))

        strategy = AIDirectorStrategy(config=default_config, litellm_bridge=bridge)
        result = await strategy.optimize_prompt(
            prompt="a cat on a table",
            reference_images=[],
            config=default_config,
            ctx=pipeline_ctx,
        )

        assert result.optimized_prompt == "a cat on a table"
        assert result.original_prompt == "a cat on a table"

    @pytest.mark.asyncio
    async def test_no_bridge_returns_original(self, default_config, pipeline_ctx):
        """Without litellm_bridge, should return original prompt immediately."""
        strategy = AIDirectorStrategy(config=default_config, litellm_bridge=None)

        result = await strategy.optimize_prompt(
            prompt="a beautiful landscape",
            reference_images=[],
            config=default_config,
            ctx=pipeline_ctx,
        )

        assert result.optimized_prompt == "a beautiful landscape"
        assert result.original_prompt == "a beautiful landscape"
        assert result.model_used is None

    @pytest.mark.asyncio
    async def test_short_prompt_expansion_with_images(self, default_config, pipeline_ctx):
        """Short prompt with reference images should include image hints."""
        config = AIDirectorConfig(
            min_prompt_length=20,  # "cat" is shorter than this
            timeout_seconds=10.0,
        )

        bridge = AsyncMock()
        bridge.completion = AsyncMock(
            return_value={
                "data": {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    "【主体】一只橘色猫咪\n"
                                    "【动作】安静地坐着\n"
                                    "【环境】温暖的室内\n"
                                    "【镜头】特写，平视角度"
                                )
                            }
                        }
                    ]
                },
                "_meta": {"cost": 0.0001},
            }
        )

        ref_image = MediaContent(
            media_type=MediaType.IMAGE,
            mime_type="image/png",
            size_bytes=1024,
            metadata={"description": "an orange tabby cat", "style": "realistic"},
        )

        strategy = AIDirectorStrategy(config=config, litellm_bridge=bridge)
        result = await strategy.optimize_prompt(
            prompt="cat",
            reference_images=[ref_image],
            config=config,
            ctx=pipeline_ctx,
        )

        # Verify the bridge was called with a message containing image hints
        call_args = bridge.completion.call_args
        messages = call_args.kwargs.get("messages") or call_args[1].get("messages", [])
        user_msg = messages[-1]["content"]
        assert "参考图片信息" in user_msg
        assert "orange tabby cat" in user_msg

        # The system prompt should be the expand one for short prompts
        system_msg = messages[0]["content"]
        assert system_msg == _EXPAND_SYSTEM_PROMPT

    @pytest.mark.asyncio
    async def test_normal_prompt_uses_rewrite_system_prompt(
        self, default_config, pipeline_ctx, mock_bridge
    ):
        """Normal (non-short) prompt should use the standard rewrite system prompt."""
        strategy = AIDirectorStrategy(
            config=default_config, litellm_bridge=mock_bridge
        )
        await strategy.optimize_prompt(
            prompt="a girl dancing in the rain with an umbrella",
            reference_images=[],
            config=default_config,
            ctx=pipeline_ctx,
        )

        call_args = mock_bridge.completion.call_args
        messages = call_args.kwargs.get("messages") or call_args[1].get("messages", [])
        system_msg = messages[0]["content"]
        assert system_msg == _REWRITE_SYSTEM_PROMPT

    @pytest.mark.asyncio
    async def test_empty_response_fallback(self, default_config, pipeline_ctx):
        """Empty model response should fallback to original prompt."""
        bridge = AsyncMock()
        bridge.completion = AsyncMock(
            return_value={
                "data": {"choices": [{"message": {"content": ""}}]},
                "_meta": {"cost": 0.0},
            }
        )

        strategy = AIDirectorStrategy(config=default_config, litellm_bridge=bridge)
        result = await strategy.optimize_prompt(
            prompt="sunset over the ocean",
            reference_images=[],
            config=default_config,
            ctx=pipeline_ctx,
        )

        assert result.optimized_prompt == "sunset over the ocean"

    @pytest.mark.asyncio
    async def test_error_in_response_fallback(self, default_config, pipeline_ctx):
        """Error response from bridge should fallback to original prompt."""
        bridge = AsyncMock()
        bridge.completion = AsyncMock(
            return_value={
                "error": {
                    "code": "model_not_found",
                    "message": "Model not registered",
                }
            }
        )

        strategy = AIDirectorStrategy(config=default_config, litellm_bridge=bridge)
        result = await strategy.optimize_prompt(
            prompt="a dog playing fetch",
            reference_images=[],
            config=default_config,
            ctx=pipeline_ctx,
        )

        assert result.optimized_prompt == "a dog playing fetch"


class TestAIDirectorStrategyApplyTemplate:
    """Tests for apply_template (placeholder)."""

    @pytest.mark.asyncio
    async def test_apply_template_placeholder(self, default_config):
        """apply_template should return empty string as placeholder."""
        strategy = AIDirectorStrategy(config=default_config)
        result = await strategy.apply_template(
            template_name="cinematic",
            variables={"subject": "a hero"},
            user_id="user-123",
        )
        assert result == ""


class TestImageHintExtraction:
    """Tests for _extract_image_hints helper."""

    def test_extracts_metadata(self, default_config):
        """Should extract description, tags, style from metadata."""
        strategy = AIDirectorStrategy(config=default_config)

        img = MediaContent(
            media_type=MediaType.IMAGE,
            mime_type="image/jpeg",
            metadata={
                "description": "forest landscape",
                "tags": ["nature", "green"],
                "style": "impressionist",
            },
        )

        hints = strategy._extract_image_hints([img])
        assert "forest landscape" in hints
        assert "nature" in hints
        assert "green" in hints
        assert "impressionist" in hints

    def test_extracts_extracted_text(self, default_config):
        """Should include extracted_text from the image."""
        strategy = AIDirectorStrategy(config=default_config)

        img = MediaContent(
            media_type=MediaType.IMAGE,
            extracted_text="A person standing in a field",
        )

        hints = strategy._extract_image_hints([img])
        assert "A person standing in a field" in hints

    def test_empty_images_returns_empty(self, default_config):
        """No images should return empty string."""
        strategy = AIDirectorStrategy(config=default_config)
        hints = strategy._extract_image_hints([])
        assert hints == ""

    def test_image_with_no_metadata_still_includes_type(self, default_config):
        """Image with minimal data should still include media_type."""
        strategy = AIDirectorStrategy(config=default_config)

        img = MediaContent(media_type=MediaType.IMAGE)
        hints = strategy._extract_image_hints([img])
        assert "image" in hints
