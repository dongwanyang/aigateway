"""
Tests for AIDirectorPlugin — AI 导演插件封装
==============================================

验证:
- 注册到 PluginRegistry (name, depends_on)
- 禁用时透传请求不做修改
- 启用时调用策略优化 prompt 并写入 ctx.extra
- 有参考图时 modality=mllm，无参考图时 modality=llm
- 创建子 span 并记录 trace_id
- 策略异常时降级到原始 prompt

需求: 1.7, 1.8, 2.10
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.context import PipelineContext
from aigateway_core.generation_optimization.config import (
    AIDirectorConfig,
    GenerationOptimizationConfig,
)
from aigateway_core.generation_optimization.models import PromptOptimizationResult
from aigateway_core.generation_optimization.plugins.ai_director_plugin import (
    AIDirectorPlugin,
    NS_GENERATION_OPTIMIZATION,
)
from aigateway_core.generation_optimization.strategies.ai_director import (
    AIDirectorStrategy,
)
from aigateway_core.media.types import MediaContent, MediaType


@pytest.fixture
def default_config():
    """Default GenerationOptimizationConfig."""
    return GenerationOptimizationConfig()


@pytest.fixture
def disabled_config():
    """Config with AI Director disabled."""
    config = GenerationOptimizationConfig()
    config.ai_director.enabled = False
    return config


@pytest.fixture
def strategy():
    """AIDirectorStrategy with no litellm_bridge."""
    return AIDirectorStrategy(config=AIDirectorConfig(), litellm_bridge=None)


@pytest.fixture
def mock_strategy():
    """Mocked AIDirectorStrategy for controlled test outputs."""
    s = MagicMock(spec=AIDirectorStrategy)
    s.optimize_prompt = AsyncMock(
        return_value=PromptOptimizationResult(
            optimized_prompt="【主体】一只猫 【动作】跳跃 【环境】花园 【镜头】中景",
            original_prompt="a cat jumping",
            model_used="gpt-4o-mini",
            cost_usd=0.001,
            duration_ms=50.0,
        )
    )
    return s


class TestAIDirectorPluginAttributes:
    """Test plugin class-level attributes."""

    def test_name(self, default_config, strategy):
        plugin = AIDirectorPlugin(strategy=strategy, config=default_config)
        assert plugin.name == "ai_director"

    def test_enabled(self, default_config, strategy):
        plugin = AIDirectorPlugin(strategy=strategy, config=default_config)
        assert plugin.enabled is True

    def test_depends_on(self, default_config, strategy):
        plugin = AIDirectorPlugin(strategy=strategy, config=default_config)
        assert plugin.depends_on == ["prompt_cache"]


class TestAIDirectorPluginDisabled:
    """Test plugin behavior when disabled."""

    @pytest.mark.asyncio
    async def test_disabled_passes_through(self, disabled_config, strategy):
        """Disabled plugin returns ctx without modification."""
        plugin = AIDirectorPlugin(strategy=strategy, config=disabled_config)
        ctx = PipelineContext(
            trace_id="test-trace",
            request={"messages": [{"role": "user", "content": "test prompt"}]}
        )

        result = await plugin.execute(ctx)

        assert result is ctx
        assert NS_GENERATION_OPTIMIZATION not in ctx.extra

    @pytest.mark.asyncio
    async def test_disabled_does_not_call_strategy(self, disabled_config, mock_strategy):
        """Disabled plugin does not invoke the strategy."""
        plugin = AIDirectorPlugin(strategy=mock_strategy, config=disabled_config)
        ctx = PipelineContext(
            trace_id="test-trace",
            request={"messages": [{"role": "user", "content": "test prompt"}]}
        )

        await plugin.execute(ctx)

        mock_strategy.optimize_prompt.assert_not_called()


class TestAIDirectorPluginEnabled:
    """Test plugin behavior when enabled."""

    @pytest.mark.asyncio
    async def test_enabled_calls_strategy(self, default_config, mock_strategy):
        """Enabled plugin invokes optimize_prompt on the strategy."""
        plugin = AIDirectorPlugin(strategy=mock_strategy, config=default_config)
        ctx = PipelineContext(
            trace_id="test-trace",
            request={"messages": [{"role": "user", "content": "a cat jumping"}]}
        )

        await plugin.execute(ctx)

        mock_strategy.optimize_prompt.assert_called_once()
        call_args = mock_strategy.optimize_prompt.call_args
        assert call_args.kwargs["prompt"] == "a cat jumping"
        assert call_args.kwargs["config"] is default_config.ai_director
        assert call_args.kwargs["ctx"] is ctx

    @pytest.mark.asyncio
    async def test_enabled_writes_result_to_extra(self, default_config, mock_strategy):
        """Plugin writes optimization result to ctx.extra."""
        plugin = AIDirectorPlugin(strategy=mock_strategy, config=default_config)
        ctx = PipelineContext(
            trace_id="test-trace",
            request={"messages": [{"role": "user", "content": "a cat jumping"}]}
        )

        await plugin.execute(ctx)

        gen_opt = ctx.extra[NS_GENERATION_OPTIMIZATION]
        ai_result = gen_opt["ai_director"]
        assert ai_result["optimized_prompt"] == "【主体】一只猫 【动作】跳跃 【环境】花园 【镜头】中景"
        assert ai_result["original_prompt"] == "a cat jumping"
        assert ai_result["model_used"] == "gpt-4o-mini"
        assert ai_result["cost_usd"] == 0.001
        assert ai_result["duration_ms"] > 0

    @pytest.mark.asyncio
    async def test_no_reference_images_modality_llm(self, default_config, mock_strategy):
        """Without reference images, modality should be 'llm'."""
        plugin = AIDirectorPlugin(strategy=mock_strategy, config=default_config)
        ctx = PipelineContext(
            trace_id="test-trace",
            request={"messages": [{"role": "user", "content": "a sunset scene"}]}
        )

        await plugin.execute(ctx)

        ai_result = ctx.extra[NS_GENERATION_OPTIMIZATION]["ai_director"]
        assert ai_result["modality"] == "llm"
        assert ai_result["has_reference_images"] is False
        assert ai_result["reference_image_count"] == 0

    @pytest.mark.asyncio
    async def test_with_reference_images_modality_mllm(self, default_config, mock_strategy):
        """With reference images, modality should be 'mllm'."""
        plugin = AIDirectorPlugin(strategy=mock_strategy, config=default_config)
        ctx = PipelineContext(
            trace_id="test-trace",
            request={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "make this vibrant"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://example.com/img.png"},
                            },
                        ],
                    }
                ]
            }
        )

        await plugin.execute(ctx)

        ai_result = ctx.extra[NS_GENERATION_OPTIMIZATION]["ai_director"]
        assert ai_result["modality"] == "mllm"
        assert ai_result["has_reference_images"] is True
        assert ai_result["reference_image_count"] == 1

    @pytest.mark.asyncio
    async def test_with_media_optimization_images(self, default_config, mock_strategy):
        """Reference images from media_optimization namespace detected."""
        plugin = AIDirectorPlugin(strategy=mock_strategy, config=default_config)
        ctx = PipelineContext(
            trace_id="test-trace",
            request={"messages": [{"role": "user", "content": "enhance"}]}
        )
        # Pre-populate media_optimization with image results
        ctx.extra["media_optimization"] = {
            "per_media_results": [
                MediaContent(media_type=MediaType.IMAGE, source_url="https://example.com/a.png"),
                MediaContent(media_type=MediaType.IMAGE, source_url="https://example.com/b.png"),
            ]
        }

        await plugin.execute(ctx)

        ai_result = ctx.extra[NS_GENERATION_OPTIMIZATION]["ai_director"]
        assert ai_result["modality"] == "mllm"
        assert ai_result["reference_image_count"] == 2


class TestAIDirectorPluginErrorHandling:
    """Test plugin error handling and fault tolerance."""

    @pytest.mark.asyncio
    async def test_strategy_exception_degrades_gracefully(self, default_config):
        """When strategy raises, plugin falls back to original prompt."""
        mock_strat = MagicMock(spec=AIDirectorStrategy)
        mock_strat.optimize_prompt = AsyncMock(side_effect=RuntimeError("model unavailable"))

        plugin = AIDirectorPlugin(strategy=mock_strat, config=default_config)
        ctx = PipelineContext(
            trace_id="test-trace",
            request={"messages": [{"role": "user", "content": "original prompt"}]}
        )

        result = await plugin.execute(ctx)

        assert result is ctx
        ai_result = ctx.extra[NS_GENERATION_OPTIMIZATION]["ai_director"]
        assert ai_result["optimized_prompt"] == "original prompt"
        assert ai_result["original_prompt"] == "original prompt"
        assert "error" in ai_result
        assert "model unavailable" in ai_result["error"]

    @pytest.mark.asyncio
    async def test_empty_messages_handled(self, default_config, mock_strategy):
        """Plugin handles empty messages gracefully."""
        plugin = AIDirectorPlugin(strategy=mock_strategy, config=default_config)
        ctx = PipelineContext(request={"messages": []}, trace_id="test-trace")

        await plugin.execute(ctx)

        # Should still call strategy with empty prompt
        call_args = mock_strategy.optimize_prompt.call_args
        assert call_args.kwargs["prompt"] == ""


class TestAIDirectorPluginTracing:
    """Test tracing/span creation."""

    @pytest.mark.asyncio
    async def test_creates_child_span(self, default_config, mock_strategy):
        """Plugin creates a child span via TracingManager."""
        plugin = AIDirectorPlugin(strategy=mock_strategy, config=default_config)
        ctx = PipelineContext(
            request={"messages": [{"role": "user", "content": "test"}]},
            trace_id="abc123trace",
        )

        with patch(
            "aigateway_core.generation_optimization.plugins.ai_director_plugin.get_tracing_manager"
        ) as mock_get_tracing:
            mock_tracing = MagicMock()
            mock_tracing.create_plugin_span.return_value = {
                "plugin_name": "ai_director",
                "started_at": 0.0,
                "attributes": {"trace.id": "abc123trace"},
            }
            mock_get_tracing.return_value = mock_tracing

            await plugin.execute(ctx)

            mock_tracing.create_plugin_span.assert_called_once_with(
                span_context={"trace_id": "abc123trace"},
                plugin_name="ai_director",
                request_id=ctx.request_id,
            )
