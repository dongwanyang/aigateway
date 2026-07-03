"""
Tests for full-pipeline tracing integration (Task 13.1).

Verifies:
1. All plugins get trace_id from ctx.trace_id
2. All plugins create child spans via TracingManager.create_plugin_span()
3. Strategy-specific attributes are recorded on spans
4. Errors are marked on spans via mark_span_error()
5. Downstream LLM calls propagate trace context via inject_trace_context()
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys
sys.path.insert(0, "aigateway-core/src")

from aigateway_core.context import PipelineContext
from aigateway_core.generation_optimization.config import (
    AIDirectorConfig,
    CostTrackingConfig,
    DraftWorkflowConfig,
    FeatureCacheConfig,
    GenerationOptimizationConfig,
    ModelRouterConfig,
    TokenCompressorConfig,
)
from aigateway_core.generation_optimization.plugins.ai_director_plugin import (
    AIDirectorPlugin,
)
from aigateway_core.generation_optimization.plugins.intent_evaluator_plugin import (
    IntentEvaluatorPlugin,
)
from aigateway_core.generation_optimization.plugins.token_compressor_plugin import (
    TokenCompressorPlugin,
)
from aigateway_core.generation_optimization.plugins.draft_generator_plugin import (
    DraftGeneratorPlugin,
)
from aigateway_core.generation_optimization.plugins.gen_model_router_plugin import (
    GenModelRouterPlugin,
)
from aigateway_core.generation_optimization.plugins.cost_tracker_plugin import (
    CostTrackerPlugin,
)
from aigateway_core.tracing import TracingManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_ctx(trace_id: str = "test-trace-123", prompt: str = "A beautiful sunset") -> PipelineContext:
    """Create a PipelineContext with a known trace_id."""
    ctx = PipelineContext(
        request={"messages": [{"role": "user", "content": prompt}]},
        trace_id=trace_id,
        request_id="req-" + uuid.uuid4().hex[:8],
    )
    return ctx


def make_config(**kwargs) -> GenerationOptimizationConfig:
    """Create a default GenerationOptimizationConfig."""
    return GenerationOptimizationConfig(**kwargs)


# ---------------------------------------------------------------------------
# Test: trace_id flows from ctx to all plugins' span creation
# ---------------------------------------------------------------------------


class TestTraceIdPropagation:
    """Verify trace_id from ctx.trace_id is used consistently by all plugins."""

    @pytest.mark.asyncio
    async def test_ai_director_uses_ctx_trace_id(self):
        """AI Director plugin creates a span using ctx.trace_id."""
        trace_id = "trace-ai-director-001"
        ctx = make_ctx(trace_id=trace_id)
        config = make_config()

        strategy = MagicMock()
        strategy.optimize_prompt = AsyncMock(
            return_value=MagicMock(
                optimized_prompt="optimized",
                original_prompt="A beautiful sunset",
                template_used=None,
                model_used="gpt-4o-mini",
                cost_usd=0.001,
            )
        )

        plugin = AIDirectorPlugin(strategy=strategy, config=config)

        with patch("aigateway_core.generation_optimization.plugins.ai_director_plugin.get_tracing_manager") as mock_tracing:
            mock_tm = MagicMock()
            mock_tm.create_plugin_span.return_value = {"trace_id": trace_id, "attributes": {}}
            mock_tracing.return_value = mock_tm

            await plugin.execute(ctx)

            # Verify create_plugin_span was called with the correct trace_id
            mock_tm.create_plugin_span.assert_called_once_with(
                span_context={"trace_id": trace_id},
                plugin_name="ai_director",
                request_id=ctx.request_id,
            )

    @pytest.mark.asyncio
    async def test_intent_evaluator_uses_ctx_trace_id(self):
        """Intent Evaluator plugin creates a span using ctx.trace_id."""
        trace_id = "trace-intent-eval-002"
        ctx = make_ctx(trace_id=trace_id)
        config = make_config()

        strategy = MagicMock()
        strategy.evaluate.return_value = MagicMock(
            score=45, factors={"subject_count": 1}, recommended_model=""
        )

        plugin = IntentEvaluatorPlugin(strategy=strategy, config=config)

        with patch("aigateway_core.generation_optimization.plugins.intent_evaluator_plugin.get_tracing_manager") as mock_tracing:
            mock_tm = MagicMock()
            mock_tm.create_plugin_span.return_value = {"trace_id": trace_id, "attributes": {}}
            mock_tracing.return_value = mock_tm

            await plugin.execute(ctx)

            mock_tm.create_plugin_span.assert_called_once_with(
                span_context={"trace_id": trace_id},
                plugin_name="intent_evaluator",
                request_id=ctx.request_id,
            )

    @pytest.mark.asyncio
    async def test_gen_model_router_uses_ctx_trace_id(self):
        """GenModelRouter plugin creates a span using ctx.trace_id."""
        trace_id = "trace-router-003"
        ctx = make_ctx(trace_id=trace_id)
        # Pre-populate intent evaluator result
        ctx.extra["generation_optimization"] = {
            "intent_evaluator": {"score": 50, "factors": {}, "recommended_model": ""}
        }
        config = make_config()

        strategy = MagicMock()
        strategy.route = AsyncMock(
            return_value=MagicMock(
                selected_model="test-model",
                selected_provider="test-provider",
                reason="complexity",
                complexity_score=50,
                estimated_cost=0.01,
            )
        )

        plugin = GenModelRouterPlugin(strategy=strategy, config=config)

        with patch("aigateway_core.generation_optimization.plugins.gen_model_router_plugin.get_tracing_manager") as mock_tracing:
            mock_tm = MagicMock()
            mock_tm.create_plugin_span.return_value = {"trace_id": trace_id, "attributes": {}}
            mock_tracing.return_value = mock_tm

            await plugin.execute(ctx)

            mock_tm.create_plugin_span.assert_called_once_with(
                span_context={"trace_id": trace_id},
                plugin_name="gen_model_router",
                request_id=ctx.request_id,
            )


# ---------------------------------------------------------------------------
# Test: mark_span_error is called on exceptions
# ---------------------------------------------------------------------------


class TestMarkSpanError:
    """Verify that mark_span_error is called when plugins encounter exceptions."""

    @pytest.mark.asyncio
    async def test_ai_director_marks_span_error(self):
        """AI Director marks span error on exception."""
        ctx = make_ctx()
        config = make_config()

        strategy = MagicMock()
        strategy.optimize_prompt = AsyncMock(side_effect=RuntimeError("LLM call failed"))

        plugin = AIDirectorPlugin(strategy=strategy, config=config)

        with patch("aigateway_core.generation_optimization.plugins.ai_director_plugin.get_tracing_manager") as mock_tracing:
            mock_tm = MagicMock()
            mock_span = MagicMock()
            mock_tm.create_plugin_span.return_value = {"trace_id": ctx.trace_id, "span": mock_span, "attributes": {}}
            mock_tracing.return_value = mock_tm

            with patch.object(TracingManager, "mark_span_error") as mock_mark_error:
                result = await plugin.execute(ctx)

                # Should have called mark_span_error
                mock_mark_error.assert_called_once()
                call_args = mock_mark_error.call_args
                assert call_args[0][0] == mock_span  # otel_span
                assert isinstance(call_args[0][1], RuntimeError)  # error

    @pytest.mark.asyncio
    async def test_intent_evaluator_marks_span_error(self):
        """Intent Evaluator marks span error on exception."""
        ctx = make_ctx()
        config = make_config()

        strategy = MagicMock()
        strategy.evaluate.side_effect = ValueError("Evaluation failed")

        plugin = IntentEvaluatorPlugin(strategy=strategy, config=config)

        with patch("aigateway_core.generation_optimization.plugins.intent_evaluator_plugin.get_tracing_manager") as mock_tracing:
            mock_tm = MagicMock()
            mock_span = MagicMock()
            mock_tm.create_plugin_span.return_value = {"trace_id": ctx.trace_id, "span": mock_span, "attributes": {}}
            mock_tracing.return_value = mock_tm

            with patch.object(TracingManager, "mark_span_error") as mock_mark_error:
                await plugin.execute(ctx)

                mock_mark_error.assert_called_once()
                call_args = mock_mark_error.call_args
                assert call_args[0][0] == mock_span
                assert isinstance(call_args[0][1], ValueError)

    @pytest.mark.asyncio
    async def test_gen_model_router_marks_span_error_on_generic_exception(self):
        """GenModelRouter marks span error on generic exception."""
        ctx = make_ctx()
        ctx.extra["generation_optimization"] = {
            "intent_evaluator": {"score": 50, "factors": {}, "recommended_model": ""}
        }
        config = make_config()

        strategy = MagicMock()
        strategy.route = AsyncMock(side_effect=RuntimeError("Router failure"))

        plugin = GenModelRouterPlugin(strategy=strategy, config=config)

        with patch("aigateway_core.generation_optimization.plugins.gen_model_router_plugin.get_tracing_manager") as mock_tracing:
            mock_tm = MagicMock()
            mock_span = MagicMock()
            mock_tm.create_plugin_span.return_value = {"trace_id": ctx.trace_id, "span": mock_span, "attributes": {}}
            mock_tracing.return_value = mock_tm

            with patch.object(TracingManager, "mark_span_error") as mock_mark_error:
                await plugin.execute(ctx)

                mock_mark_error.assert_called_once()
                call_args = mock_mark_error.call_args
                assert call_args[0][0] == mock_span
                assert isinstance(call_args[0][1], RuntimeError)

    @pytest.mark.asyncio
    async def test_cost_tracker_marks_span_error(self):
        """Cost Tracker marks span error on exception."""
        ctx = make_ctx()
        config = make_config()

        # CostTrackerPlugin expects a GenerationCostTracker instance
        tracker = MagicMock()
        tracker.record_total_saving.side_effect = ZeroDivisionError("division error")

        plugin = CostTrackerPlugin(tracker=tracker, config=config)

        with patch("aigateway_core.generation_optimization.plugins.cost_tracker_plugin.get_tracing_manager") as mock_tracing:
            mock_tm = MagicMock()
            mock_span = MagicMock()
            mock_tm.create_plugin_span.return_value = {"trace_id": ctx.trace_id, "span": mock_span, "attributes": {}}
            mock_tracing.return_value = mock_tm

            with patch.object(TracingManager, "mark_span_error") as mock_mark_error:
                await plugin.execute(ctx)

                mock_mark_error.assert_called_once()
                call_args = mock_mark_error.call_args
                assert call_args[0][0] == mock_span
                assert isinstance(call_args[0][1], ZeroDivisionError)


# ---------------------------------------------------------------------------
# Test: inject_trace_context propagates trace_id in downstream LLM calls
# ---------------------------------------------------------------------------


class TestInjectTraceContext:
    """Verify that inject_trace_context is called for downstream LLM calls."""

    @pytest.mark.asyncio
    async def test_ai_director_strategy_injects_trace_context(self):
        """AI Director strategy injects trace context headers into LLM calls."""
        from aigateway_core.generation_optimization.strategies.ai_director import (
            AIDirectorStrategy,
        )

        trace_id = "trace-inject-005"
        ctx = make_ctx(trace_id=trace_id)
        ai_config = AIDirectorConfig()

        # Mock litellm_bridge
        mock_bridge = MagicMock()
        mock_bridge.completion = AsyncMock(return_value={
            "data": {
                "choices": [{"message": {"content": "optimized prompt"}}]
            },
            "_meta": {"cost": 0.001},
        })

        strategy = AIDirectorStrategy(config=ai_config, litellm_bridge=mock_bridge)

        result = await strategy.optimize_prompt(
            prompt="A beautiful sunset over the ocean with dramatic clouds",
            reference_images=[],
            config=ai_config,
            ctx=ctx,
        )

        # Verify completion was called with extra_headers containing trace context
        mock_bridge.completion.assert_called_once()
        call_kwargs = mock_bridge.completion.call_args[1]
        extra_headers = call_kwargs.get("extra_headers", {})

        assert "traceparent" in extra_headers
        assert "X-Trace-ID" in extra_headers
        assert extra_headers["X-Trace-ID"] == trace_id
        assert trace_id in extra_headers["traceparent"]

    def test_inject_trace_context_format(self):
        """inject_trace_context produces correct W3C traceparent format."""
        headers: Dict[str, str] = {}
        TracingManager.inject_trace_context(
            headers=headers,
            trace_id="abc123",
            span_id="def456",
        )

        assert headers["traceparent"] == "00-abc123-def456-01"
        assert headers["X-Trace-ID"] == "abc123"
        assert headers["X-Span-ID"] == "def456"


# ---------------------------------------------------------------------------
# Test: Strategy-specific attributes recorded on spans
# ---------------------------------------------------------------------------


class TestSpanAttributes:
    """Verify strategy-specific attributes are recorded on child spans."""

    @pytest.mark.asyncio
    async def test_ai_director_records_span_attributes(self):
        """AI Director records model_used, modality, prompt_length on span."""
        ctx = make_ctx()
        config = make_config()

        strategy = MagicMock()
        strategy.optimize_prompt = AsyncMock(
            return_value=MagicMock(
                optimized_prompt="detailed structured prompt",
                original_prompt="A beautiful sunset",
                template_used=None,
                model_used="gpt-4o-mini",
                cost_usd=0.001,
            )
        )

        plugin = AIDirectorPlugin(strategy=strategy, config=config)

        with patch("aigateway_core.generation_optimization.plugins.ai_director_plugin.get_tracing_manager") as mock_tracing:
            mock_tm = MagicMock()
            span_attrs = {}
            mock_tm.create_plugin_span.return_value = {"trace_id": ctx.trace_id, "attributes": span_attrs}
            mock_tracing.return_value = mock_tm

            await plugin.execute(ctx)

            assert "ai_director.model_used" in span_attrs
            assert span_attrs["ai_director.model_used"] == "gpt-4o-mini"
            assert "ai_director.modality" in span_attrs
            assert "ai_director.prompt_length" in span_attrs
            assert "ai_director.duration_ms" in span_attrs

    @pytest.mark.asyncio
    async def test_intent_evaluator_records_complexity_score(self):
        """Intent Evaluator records complexity_score on span."""
        ctx = make_ctx()
        config = make_config()

        strategy = MagicMock()
        strategy.evaluate.return_value = MagicMock(
            score=72, factors={"subject_count": 2}, recommended_model="high-end"
        )

        plugin = IntentEvaluatorPlugin(strategy=strategy, config=config)

        with patch("aigateway_core.generation_optimization.plugins.intent_evaluator_plugin.get_tracing_manager") as mock_tracing:
            mock_tm = MagicMock()
            span_attrs = {}
            mock_tm.create_plugin_span.return_value = {"trace_id": ctx.trace_id, "attributes": span_attrs}
            mock_tracing.return_value = mock_tm

            await plugin.execute(ctx)

            assert span_attrs["intent_evaluator.complexity_score"] == 72

    @pytest.mark.asyncio
    async def test_gen_model_router_records_routing_decision(self):
        """GenModelRouter records routing_decision attributes on span."""
        ctx = make_ctx()
        ctx.extra["generation_optimization"] = {
            "intent_evaluator": {"score": 60, "factors": {}, "recommended_model": ""}
        }
        config = make_config()

        strategy = MagicMock()
        strategy.route = AsyncMock(
            return_value=MagicMock(
                selected_model="agnes-image-2.1-flash",
                selected_provider="agnes",
                reason="complexity",
                complexity_score=60,
                estimated_cost=0.05,
            )
        )

        plugin = GenModelRouterPlugin(strategy=strategy, config=config)

        with patch("aigateway_core.generation_optimization.plugins.gen_model_router_plugin.get_tracing_manager") as mock_tracing:
            mock_tm = MagicMock()
            span_attrs = {}
            mock_tm.create_plugin_span.return_value = {"trace_id": ctx.trace_id, "attributes": span_attrs}
            mock_tracing.return_value = mock_tm

            await plugin.execute(ctx)

            assert span_attrs["gen_model_router.selected_model"] == "agnes-image-2.1-flash"
            assert span_attrs["gen_model_router.selected_provider"] == "agnes"
            assert span_attrs["gen_model_router.reason"] == "complexity"
            assert span_attrs["gen_model_router.complexity_score"] == 60
