"""
Tests for IntentEvaluatorPlugin — 意图评估插件封装
===================================================

验证:
- 注册到 PluginRegistry (name, depends_on)
- 禁用时透传请求不做修改
- 启用时调用策略评估复杂度并写入 ctx.extra
- 优先使用 AI Director 优化后的 prompt
- 创建子 span 并记录 complexity_score
- 策略异常时降级到默认分数 (50)

需求: 2.7, 2.8, 1.8
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.pipelines.generation._common.config import (
    GenerationOptimizationConfig,
    ModelRouterConfig,
)
from aigateway_core.pipelines.generation._common.models import ComplexityEvaluation
from aigateway_core.pipelines.generation.intent.intent_evaluator_plugin import (
    DEFAULT_COMPLEXITY_SCORE,
    IntentEvaluatorPlugin,
    NS_GENERATION_OPTIMIZATION,
)
from aigateway_core.pipelines.generation.intent.intent_evaluator import (
    IntentEvaluatorStrategy,
)


@pytest.fixture
def default_config():
    """Default GenerationOptimizationConfig."""
    return GenerationOptimizationConfig()


@pytest.fixture
def disabled_config():
    """Config with model router disabled."""
    config = GenerationOptimizationConfig()
    config.model_router.enabled = False
    return config


@pytest.fixture
def strategy():
    """IntentEvaluatorStrategy with default config."""
    return IntentEvaluatorStrategy(config=ModelRouterConfig())


@pytest.fixture
def mock_strategy():
    """Mocked IntentEvaluatorStrategy."""
    s = MagicMock(spec=IntentEvaluatorStrategy)
    s.evaluate.return_value = ComplexityEvaluation(
        score=65,
        factors={
            "subject_count": 15,
            "interaction_type": 30,
            "camera_movement": 10,
            "target_resolution": 10,
        },
        recommended_model="",
    )
    return s


class TestIntentEvaluatorPluginAttributes:
    """Test plugin class-level attributes."""

    def test_name(self, default_config, strategy):
        plugin = IntentEvaluatorPlugin(strategy=strategy, config=default_config)
        assert plugin.name == "intent_evaluator"

    def test_enabled(self, default_config, strategy):
        plugin = IntentEvaluatorPlugin(strategy=strategy, config=default_config)
        assert plugin.enabled is True

    def test_depends_on(self, default_config, strategy):
        plugin = IntentEvaluatorPlugin(strategy=strategy, config=default_config)
        assert plugin.depends_on == ["ai_director"]


class TestIntentEvaluatorPluginDisabled:
    """Test plugin behavior when disabled."""

    @pytest.mark.asyncio
    async def test_disabled_passes_through(self, disabled_config, strategy):
        """Disabled plugin returns ctx without modification."""
        plugin = IntentEvaluatorPlugin(strategy=strategy, config=disabled_config)
        ctx = PipelineContext(
            trace_id="test-trace",
            request={"messages": [{"role": "user", "content": "test prompt"}]}
        )

        result = await plugin.execute(ctx)

        assert result is ctx
        assert result.should_stop is False
        assert result.response is None
        # Should not write intent_evaluator data
        gen_opt = ctx.extra.get(NS_GENERATION_OPTIMIZATION, {})
        assert "intent_evaluator" not in gen_opt

    @pytest.mark.asyncio
    async def test_disabled_does_not_call_strategy(self, disabled_config, mock_strategy):
        """Disabled plugin does not invoke the strategy."""
        plugin = IntentEvaluatorPlugin(strategy=mock_strategy, config=disabled_config)
        ctx = PipelineContext(
            trace_id="test-trace",
            request={"messages": [{"role": "user", "content": "test prompt"}]}
        )

        await plugin.execute(ctx)

        mock_strategy.evaluate.assert_not_called()
        # Also verify ctx.extra was not modified
        assert "intent_evaluator" not in ctx.extra.get(NS_GENERATION_OPTIMIZATION, {})


class TestIntentEvaluatorPluginEnabled:
    """Test plugin behavior when enabled."""

    @pytest.mark.asyncio
    async def test_enabled_calls_strategy(self, default_config, mock_strategy):
        """Enabled plugin invokes evaluate on the strategy."""
        plugin = IntentEvaluatorPlugin(strategy=mock_strategy, config=default_config)
        ctx = PipelineContext(
            trace_id="test-trace",
            request={"messages": [{"role": "user", "content": "two cats fighting"}]}
        )

        await plugin.execute(ctx)

        mock_strategy.evaluate.assert_called_once()
        call_args = mock_strategy.evaluate.call_args
        assert call_args.kwargs["prompt"] == "two cats fighting"

    @pytest.mark.asyncio
    async def test_enabled_writes_result_to_extra(self, default_config, mock_strategy):
        """Plugin writes evaluation result to ctx.extra."""
        plugin = IntentEvaluatorPlugin(strategy=mock_strategy, config=default_config)
        ctx = PipelineContext(
            trace_id="test-trace",
            request={"messages": [{"role": "user", "content": "two cats fighting"}]}
        )

        await plugin.execute(ctx)

        gen_opt = ctx.extra[NS_GENERATION_OPTIMIZATION]
        ie_result = gen_opt["intent_evaluator"]
        assert ie_result["score"] == 65
        assert ie_result["factors"]["subject_count"] == 15
        assert ie_result["factors"]["interaction_type"] == 30
        assert ie_result["duration_ms"] > 0

    @pytest.mark.asyncio
    async def test_uses_ai_director_optimized_prompt(self, default_config, mock_strategy):
        """Plugin prefers the optimized prompt from AI Director."""
        plugin = IntentEvaluatorPlugin(strategy=mock_strategy, config=default_config)
        ctx = PipelineContext(
            trace_id="test-trace",
            request={"messages": [{"role": "user", "content": "original prompt"}]}
        )
        # Pre-populate AI Director result
        ctx.extra[NS_GENERATION_OPTIMIZATION] = {
            "ai_director": {
                "optimized_prompt": "【主体】两只猫 【动作】打斗 【镜头】跟踪",
                "original_prompt": "original prompt",
            }
        }

        await plugin.execute(ctx)

        call_args = mock_strategy.evaluate.call_args
        assert call_args.kwargs["prompt"] == "【主体】两只猫 【动作】打斗 【镜头】跟踪"

    @pytest.mark.asyncio
    async def test_falls_back_to_request_prompt_without_ai_director(
        self, default_config, mock_strategy
    ):
        """Without AI Director result, uses the original request prompt."""
        plugin = IntentEvaluatorPlugin(strategy=mock_strategy, config=default_config)
        ctx = PipelineContext(
            trace_id="test-trace",
            request={"messages": [{"role": "user", "content": "a cat sleeping"}]}
        )

        await plugin.execute(ctx)

        call_args = mock_strategy.evaluate.call_args
        assert call_args.kwargs["prompt"] == "a cat sleeping"

    @pytest.mark.asyncio
    async def test_extracts_generation_params_from_request(
        self, default_config, mock_strategy
    ):
        """Plugin extracts generation params like width/height from request."""
        plugin = IntentEvaluatorPlugin(strategy=mock_strategy, config=default_config)
        ctx = PipelineContext(
            trace_id="test-trace",
            request={
                "messages": [{"role": "user", "content": "a landscape"}],
                "width": 1920,
                "height": 1080,
            }
        )

        await plugin.execute(ctx)

        call_args = mock_strategy.evaluate.call_args
        gen_params = call_args.kwargs["generation_params"]
        assert gen_params["width"] == 1920
        assert gen_params["height"] == 1080

    @pytest.mark.asyncio
    async def test_extracts_size_param_openai_format(self, default_config, mock_strategy):
        """Plugin handles OpenAI-format 'size' parameter."""
        plugin = IntentEvaluatorPlugin(strategy=mock_strategy, config=default_config)
        ctx = PipelineContext(
            trace_id="test-trace",
            request={
                "messages": [{"role": "user", "content": "generate image"}],
                "size": "1024x1024",
            }
        )

        await plugin.execute(ctx)

        call_args = mock_strategy.evaluate.call_args
        gen_params = call_args.kwargs["generation_params"]
        assert gen_params["width"] == 1024
        assert gen_params["height"] == 1024


class TestIntentEvaluatorPluginErrorHandling:
    """Test plugin error handling and fault tolerance."""

    @pytest.mark.asyncio
    async def test_strategy_exception_uses_default_score(self, default_config):
        """When strategy raises, plugin falls back to default score."""
        mock_strat = MagicMock(spec=IntentEvaluatorStrategy)
        mock_strat.evaluate.side_effect = RuntimeError("evaluation failed")

        plugin = IntentEvaluatorPlugin(strategy=mock_strat, config=default_config)
        ctx = PipelineContext(
            trace_id="test-trace",
            request={"messages": [{"role": "user", "content": "test prompt"}]}
        )

        result = await plugin.execute(ctx)

        assert result is ctx
        ie_result = ctx.extra[NS_GENERATION_OPTIMIZATION]["intent_evaluator"]
        assert ie_result["score"] == DEFAULT_COMPLEXITY_SCORE
        assert ie_result["factors"] == {}
        assert "error" in ie_result
        assert "evaluation failed" in ie_result["error"]

    @pytest.mark.asyncio
    async def test_empty_messages_handled(self, default_config, mock_strategy):
        """Plugin handles empty messages gracefully."""
        plugin = IntentEvaluatorPlugin(strategy=mock_strategy, config=default_config)
        ctx = PipelineContext(request={"messages": []}, trace_id="test-trace")

        await plugin.execute(ctx)

        # Should still call strategy with empty prompt
        call_args = mock_strategy.evaluate.call_args
        assert call_args.kwargs["prompt"] == ""


class TestIntentEvaluatorPluginTracing:
    """Test plugin TraceEvent emission (post Task 7: span → PipelineEngine auto-instrument)."""

    @pytest.mark.asyncio
    async def test_emits_plugin_trace_event(self, default_config, mock_strategy):
        """PipelineEngine emits a kind='plugin' TraceEvent on plugin success."""
        from aigateway_core.shared.trace_event import TraceCollector

        plugin = IntentEvaluatorPlugin(strategy=mock_strategy, config=default_config)
        ctx = PipelineContext(
            request={"messages": [{"role": "user", "content": "test"}]},
            trace_id="trace123",
        )
        collector = TraceCollector.start("trace123")

        # Gen-opt plugins no longer emit TraceEvents directly; PipelineEngine wraps execute().
        from aigateway_core.dispatch.pipeline_engine import PipelineEngine
        engine = PipelineEngine(registry=MagicMock(), pipeline_kind="generation")
        engine._ordered_plugins = [plugin]
        engine._initialized = True
        await engine.execute_ctx(ctx)

        events = [e for e in collector.events if e.kind == "plugin" and e.stage == "intent_evaluator"]
        assert len(events) == 1
        assert events[0].trace_id == "trace123"
        assert events[0].status == "ok"

    @pytest.mark.asyncio
    async def test_writes_complexity_score_to_context(self, default_config, mock_strategy):
        """Plugin records complexity_score in ctx.extra (replaces span attrs)."""
        from aigateway_core.shared.trace_event import TraceCollector

        plugin = IntentEvaluatorPlugin(strategy=mock_strategy, config=default_config)
        ctx = PipelineContext(
            request={"messages": [{"role": "user", "content": "test"}]},
            trace_id="trace456",
        )
        collector = TraceCollector.start("trace456")

        await plugin.execute(ctx)

        result = ctx.extra["generation_optimization"]["intent_evaluator"]
        assert result["score"] == 65
