"""
Tests for GenModelRouterPlugin — 智能模型路由插件封装
=====================================================

验证:
- 注册到 PluginRegistry (name, depends_on)
- 禁用时透传请求不做修改
- 启用时调用策略路由并写入 ctx.extra 和 ctx.model_router
- 路由决策记录到请求元数据（模型、provider、原因、分数）
- ModelRoutingError 时返回错误响应并停止管线
- 其他异常时回退到 default_model 并记录日志
- 创建子 span 并记录路由属性

需求: 2.7, 2.8, 1.8
"""

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.context import PipelineContext
from aigateway_core.generation_optimization.config import (
    GenerationOptimizationConfig,
    ModelRouterConfig,
)
from aigateway_core.generation_optimization.exceptions import ModelRoutingError
from aigateway_core.generation_optimization.models import RoutingDecision
from aigateway_core.generation_optimization.plugins.gen_model_router_plugin import (
    GenModelRouterPlugin,
    NS_GENERATION_OPTIMIZATION,
)
from aigateway_core.generation_optimization.strategies.model_router import (
    ModelRouterStrategy,
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
def mock_strategy():
    """Mocked ModelRouterStrategy."""
    s = MagicMock(spec=ModelRouterStrategy)
    s.route = AsyncMock(
        return_value=RoutingDecision(
            selected_model="agnes-image-2.1-flash",
            selected_provider="agnes",
            reason="complexity",
            complexity_score=65,
            estimated_cost=0.05,
        )
    )
    return s


@pytest.fixture
def ctx_with_intent_result():
    """PipelineContext with intent_evaluator result pre-populated."""
    ctx = PipelineContext(
        request={"messages": [{"role": "user", "content": "two cats fighting"}]}
    )
    ctx.extra[NS_GENERATION_OPTIMIZATION] = {
        "intent_evaluator": {
            "score": 65,
            "factors": {"subject_count": 15, "interaction_type": 30},
            "recommended_model": "",
            "duration_ms": 5.0,
        }
    }
    return ctx


class TestGenModelRouterPluginAttributes:
    """Test plugin class-level attributes."""

    def test_name(self, default_config, mock_strategy):
        plugin = GenModelRouterPlugin(strategy=mock_strategy, config=default_config)
        assert plugin.name == "gen_model_router"

    def test_enabled(self, default_config, mock_strategy):
        plugin = GenModelRouterPlugin(strategy=mock_strategy, config=default_config)
        assert plugin.enabled is True

    def test_depends_on(self, default_config, mock_strategy):
        plugin = GenModelRouterPlugin(strategy=mock_strategy, config=default_config)
        assert plugin.depends_on == ["draft_generator"]


class TestGenModelRouterPluginDisabled:
    """Test plugin behavior when disabled."""

    @pytest.mark.asyncio
    async def test_disabled_passes_through(self, disabled_config, mock_strategy):
        """Disabled plugin returns ctx without modification."""
        plugin = GenModelRouterPlugin(strategy=mock_strategy, config=disabled_config)
        ctx = PipelineContext(
            request={"messages": [{"role": "user", "content": "test prompt"}]}
        )

        result = await plugin.execute(ctx)

        assert result is ctx
        gen_opt = ctx.extra.get(NS_GENERATION_OPTIMIZATION, {})
        assert "model_router" not in gen_opt

    @pytest.mark.asyncio
    async def test_disabled_does_not_call_strategy(self, disabled_config, mock_strategy):
        """Disabled plugin does not invoke the strategy."""
        plugin = GenModelRouterPlugin(strategy=mock_strategy, config=disabled_config)
        ctx = PipelineContext(
            request={"messages": [{"role": "user", "content": "test"}]}
        )

        await plugin.execute(ctx)

        mock_strategy.route.assert_not_called()


class TestGenModelRouterPluginEnabled:
    """Test plugin behavior when enabled."""

    @pytest.mark.asyncio
    async def test_enabled_calls_strategy(
        self, default_config, mock_strategy, ctx_with_intent_result
    ):
        """Enabled plugin invokes route on the strategy."""
        plugin = GenModelRouterPlugin(strategy=mock_strategy, config=default_config)

        await plugin.execute(ctx_with_intent_result)

        mock_strategy.route.assert_called_once()
        call_args = mock_strategy.route.call_args
        assert call_args.kwargs["complexity_score"] == 65
        assert call_args.kwargs["required_modality"] == "generative"

    @pytest.mark.asyncio
    async def test_writes_routing_decision_to_extra(
        self, default_config, mock_strategy, ctx_with_intent_result
    ):
        """Plugin writes routing decision to ctx.extra."""
        plugin = GenModelRouterPlugin(strategy=mock_strategy, config=default_config)

        await plugin.execute(ctx_with_intent_result)

        gen_opt = ctx_with_intent_result.extra[NS_GENERATION_OPTIMIZATION]
        router_result = gen_opt["model_router"]
        assert router_result["selected_model"] == "agnes-image-2.1-flash"
        assert router_result["selected_provider"] == "agnes"
        assert router_result["reason"] == "complexity"
        assert router_result["complexity_score"] == 65
        assert router_result["estimated_cost"] == 0.05
        assert router_result["duration_ms"] > 0

    @pytest.mark.asyncio
    async def test_writes_to_ctx_model_router(
        self, default_config, mock_strategy, ctx_with_intent_result
    ):
        """Plugin writes selected_model to ctx.model_router for downstream."""
        plugin = GenModelRouterPlugin(strategy=mock_strategy, config=default_config)

        await plugin.execute(ctx_with_intent_result)

        assert ctx_with_intent_result.model_router["selected_model"] == "agnes-image-2.1-flash"
        assert ctx_with_intent_result.model_router["selected_provider"] == "agnes"

    @pytest.mark.asyncio
    async def test_passes_routing_hint(self, default_config, mock_strategy):
        """Plugin passes routing_hint from request to strategy."""
        plugin = GenModelRouterPlugin(strategy=mock_strategy, config=default_config)
        ctx = PipelineContext(
            request={
                "messages": [{"role": "user", "content": "test"}],
                "routing_hint": "best quality",
            }
        )
        ctx.extra[NS_GENERATION_OPTIMIZATION] = {
            "intent_evaluator": {"score": 40, "factors": {}, "duration_ms": 1.0}
        }

        await plugin.execute(ctx)

        call_args = mock_strategy.route.call_args
        assert call_args.kwargs["routing_hint"] == "best quality"

    @pytest.mark.asyncio
    async def test_passes_model_override(self, default_config, mock_strategy):
        """Plugin passes model_override (target_model) from request to strategy."""
        plugin = GenModelRouterPlugin(strategy=mock_strategy, config=default_config)
        ctx = PipelineContext(
            request={
                "messages": [{"role": "user", "content": "test"}],
                "target_model": "agnes-video-v2.0",
            }
        )
        ctx.extra[NS_GENERATION_OPTIMIZATION] = {
            "intent_evaluator": {"score": 80, "factors": {}, "duration_ms": 1.0}
        }

        await plugin.execute(ctx)

        call_args = mock_strategy.route.call_args
        assert call_args.kwargs["model_override"] == "agnes-video-v2.0"

    @pytest.mark.asyncio
    async def test_uses_required_modality_from_request(self, default_config, mock_strategy):
        """Plugin reads required_modality from request."""
        plugin = GenModelRouterPlugin(strategy=mock_strategy, config=default_config)
        ctx = PipelineContext(
            request={
                "messages": [{"role": "user", "content": "test"}],
                "required_modality": "mllm",
            }
        )
        ctx.extra[NS_GENERATION_OPTIMIZATION] = {
            "intent_evaluator": {"score": 30, "factors": {}, "duration_ms": 1.0}
        }

        await plugin.execute(ctx)

        call_args = mock_strategy.route.call_args
        assert call_args.kwargs["required_modality"] == "mllm"


class TestGenModelRouterPluginErrorHandling:
    """Test plugin error handling and fault tolerance."""

    @pytest.mark.asyncio
    async def test_model_routing_error_sets_error_response(self, default_config):
        """ModelRoutingError causes error response and stops pipeline."""
        mock_strat = MagicMock(spec=ModelRouterStrategy)
        mock_strat.route = AsyncMock(
            side_effect=ModelRoutingError("模型 'unknown-model' 不存在")
        )

        plugin = GenModelRouterPlugin(strategy=mock_strat, config=default_config)
        ctx = PipelineContext(
            request={"messages": [{"role": "user", "content": "test"}]}
        )
        ctx.extra[NS_GENERATION_OPTIMIZATION] = {
            "intent_evaluator": {"score": 50, "factors": {}, "duration_ms": 1.0}
        }

        result = await plugin.execute(ctx)

        assert result.should_stop is True
        assert result.response is not None
        error_body = json.loads(result.response)
        assert "error" in error_body
        assert error_body["error"]["type"] == "model_routing_error"
        assert "unknown-model" in error_body["error"]["message"]

    @pytest.mark.asyncio
    async def test_generic_exception_falls_back_to_default_model(self, default_config):
        """Generic exceptions cause fallback to default_model."""
        mock_strat = MagicMock(spec=ModelRouterStrategy)
        mock_strat.route = AsyncMock(
            side_effect=RuntimeError("unexpected network error")
        )

        plugin = GenModelRouterPlugin(strategy=mock_strat, config=default_config)
        ctx = PipelineContext(
            request={"messages": [{"role": "user", "content": "test"}]}
        )
        ctx.extra[NS_GENERATION_OPTIMIZATION] = {
            "intent_evaluator": {"score": 60, "factors": {}, "duration_ms": 1.0}
        }

        result = await plugin.execute(ctx)

        # Should NOT stop the pipeline
        assert result.should_stop is False
        assert result.response is None

        # Should fallback to default model
        gen_opt = result.extra[NS_GENERATION_OPTIMIZATION]
        router_result = gen_opt["model_router"]
        assert router_result["selected_model"] == "agnes-2.0-flash"
        assert router_result["reason"] == "fallback"
        assert "error" in router_result
        assert "unexpected network error" in router_result["error"]

        # Should also write to ctx.model_router
        assert result.model_router["selected_model"] == "agnes-2.0-flash"

    @pytest.mark.asyncio
    async def test_missing_intent_evaluator_result_falls_back(self, default_config):
        """Missing intent_evaluator score triggers fallback to default_model."""
        mock_strat = MagicMock(spec=ModelRouterStrategy)
        mock_strat.route = AsyncMock()

        plugin = GenModelRouterPlugin(strategy=mock_strat, config=default_config)
        ctx = PipelineContext(
            request={"messages": [{"role": "user", "content": "test"}]}
        )
        # No intent_evaluator result in context

        result = await plugin.execute(ctx)

        # Should fallback because _get_complexity_score raises KeyError
        assert result.should_stop is False
        gen_opt = result.extra[NS_GENERATION_OPTIMIZATION]
        router_result = gen_opt["model_router"]
        assert router_result["selected_model"] == "agnes-2.0-flash"
        assert router_result["reason"] == "fallback"


class TestGenModelRouterPluginTracing:
    """Test tracing/span creation."""

    @pytest.mark.asyncio
    async def test_creates_child_span(
        self, default_config, mock_strategy, ctx_with_intent_result
    ):
        """Plugin creates a child span via TracingManager."""
        plugin = GenModelRouterPlugin(strategy=mock_strategy, config=default_config)
        ctx_with_intent_result.trace_id = "routertrace789"

        with patch(
            "aigateway_core.generation_optimization.plugins.gen_model_router_plugin.get_tracing_manager"
        ) as mock_get_tracing:
            mock_tracing = MagicMock()
            mock_tracing.create_plugin_span.return_value = {
                "plugin_name": "gen_model_router",
                "started_at": 0.0,
                "attributes": {"trace.id": "routertrace789"},
            }
            mock_get_tracing.return_value = mock_tracing

            await plugin.execute(ctx_with_intent_result)

            mock_tracing.create_plugin_span.assert_called_once_with(
                span_context={"trace_id": "routertrace789"},
                plugin_name="gen_model_router",
                request_id=ctx_with_intent_result.request_id,
            )

    @pytest.mark.asyncio
    async def test_span_records_routing_attributes(
        self, default_config, mock_strategy, ctx_with_intent_result
    ):
        """Plugin records model, provider, reason, score in span attributes."""
        plugin = GenModelRouterPlugin(strategy=mock_strategy, config=default_config)
        ctx_with_intent_result.trace_id = "traceXYZ"

        span_attrs = {}
        with patch(
            "aigateway_core.generation_optimization.plugins.gen_model_router_plugin.get_tracing_manager"
        ) as mock_get_tracing:
            mock_tracing = MagicMock()
            mock_tracing.create_plugin_span.return_value = {
                "plugin_name": "gen_model_router",
                "started_at": 0.0,
                "attributes": span_attrs,
            }
            mock_get_tracing.return_value = mock_tracing

            await plugin.execute(ctx_with_intent_result)

        assert span_attrs["gen_model_router.selected_model"] == "agnes-image-2.1-flash"
        assert span_attrs["gen_model_router.selected_provider"] == "agnes"
        assert span_attrs["gen_model_router.reason"] == "complexity"
        assert span_attrs["gen_model_router.complexity_score"] == 65
        assert span_attrs["gen_model_router.estimated_cost"] == 0.05
