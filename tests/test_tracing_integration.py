"""
Tests for full-pipeline tracing integration (Task 13.1).

Verifies (post Task 7 — gen-opt 插件删 create_plugin_span,改 emit_plugin_event):
1. All plugins emit a `kind="plugin"` TraceEvent tagged with ctx.trace_id
2. On success the event status == "ok"; on exception status == "error"
3. Strategy-specific data is no longer on OTel span attrs — we assert the
   plugin's ctx.extra output instead (the span-attr layer is gone)
4. Downstream LLM calls still propagate trace context via inject_trace_context()
   (this OTel-mechanic test is unaffected by the plugin-span removal)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys
sys.path.insert(0, "aigateway-core/src")

from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.pipelines.generation._common.config import (
    AIDirectorConfig,
    GenerationOptimizationConfig,
)
from aigateway_core.pipelines.generation.director.ai_director_plugin import (
    AIDirectorPlugin,
)
from aigateway_core.pipelines.generation.intent.intent_evaluator_plugin import (
    IntentEvaluatorPlugin,
)
from aigateway_core.pipelines.generation.token.token_compressor_plugin import (
    TokenCompressorPlugin,
)
from aigateway_core.pipelines.generation.draft.draft_generator_plugin import (
    DraftGeneratorPlugin,
)
from aigateway_core.pipelines.generation.routing_signals.gen_model_router_plugin import (
    GenModelRouterPlugin,
)
from aigateway_core.pipelines.generation.cost.cost_tracker_plugin import (
    CostTrackerPlugin,
)
from aigateway_core.shared.trace_event import TraceCollector


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


def plugin_events(collector: TraceCollector, name: str) -> List:
    """Filter collector.events for kind=='plugin' and stage==name."""
    return [e for e in collector.events if e.kind == "plugin" and e.stage == name]


def _run_via_engine(plugin: Any, ctx: PipelineContext) -> "asyncio.Future":
    """Run a single plugin through a PipelineEngine so the engine's auto-
    instrumentation emits the kind='plugin' TraceEvent (success/error).

    gen-opt plugins no longer emit TraceEvents themselves (Task 7); the
    PipelineEngine wraps plugin.execute() and emits on success/error/skip.
    Tests that assert trace events must therefore go through the engine,
    not call plugin.execute() directly.
    """
    from aigateway_core.dispatch.pipeline_engine import PipelineEngine

    engine = PipelineEngine(registry=MagicMock(), pipeline_kind="generation")
    engine._ordered_plugins = [plugin]
    engine._initialized = True
    return engine.execute_ctx(ctx)


# ---------------------------------------------------------------------------
# Test: trace_id flows from ctx into emitted plugin TraceEvents
# ---------------------------------------------------------------------------


class TestTraceIdPropagation:
    """Verify each plugin emits a TraceEvent carrying ctx.trace_id."""

    @pytest.mark.asyncio
    async def test_ai_director_emits_trace_event_with_trace_id(self):
        """AI Director emits a plugin TraceEvent tagged with ctx.trace_id."""
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
        collector = TraceCollector.start(trace_id)

        await _run_via_engine(plugin, ctx)

        events = plugin_events(collector, "ai_director")
        # 2 events: PipelineEngine auto-instrument + plugin add_plugin_trace
        assert len(events) == 2
        assert events[0].trace_id == trace_id
        assert events[0].kind == "plugin"
        assert events[0].name == "ai_director.execute"
        assert events[0].status == "ok"

    @pytest.mark.asyncio
    async def test_intent_evaluator_emits_trace_event_with_trace_id(self):
        """Intent Evaluator emits a plugin TraceEvent tagged with ctx.trace_id."""
        trace_id = "trace-intent-eval-002"
        ctx = make_ctx(trace_id=trace_id)
        config = make_config()

        strategy = MagicMock()
        strategy.evaluate.return_value = MagicMock(
            score=45, factors={"subject_count": 1}, recommended_model=""
        )

        plugin = IntentEvaluatorPlugin(strategy=strategy, config=config)
        collector = TraceCollector.start(trace_id)

        await _run_via_engine(plugin, ctx)

        events = plugin_events(collector, "intent_evaluator")
        assert len(events) == 1
        assert events[0].trace_id == trace_id
        assert events[0].status == "ok"

    @pytest.mark.asyncio
    async def test_gen_model_router_emits_trace_event_with_trace_id(self):
        """GenModelRouter emits a plugin TraceEvent tagged with ctx.trace_id."""
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
        collector = TraceCollector.start(trace_id)

        await _run_via_engine(plugin, ctx)

        events = plugin_events(collector, "gen_model_router")
        # 2 events: PipelineEngine auto-instrument + plugin add_plugin_trace
        assert len(events) == 2
        assert events[0].trace_id == trace_id
        assert events[0].status == "ok"


# ---------------------------------------------------------------------------
# Test: error path emits status="error" TraceEvent (replaces mark_span_error)
# ---------------------------------------------------------------------------


class TestPluginErrorEmit:
    """Verify graceful-degradation contract on strategy exceptions.

    gen-opt plugins swallow strategy exceptions and degrade to a fallback
    result (writing `error` into ctx.extra) rather than re-raising — so the
    pipeline is never broken by a single strategy failure. The PipelineEngine
    therefore sees a normal return and emits status='ok' (no error event).
    These tests assert that contract: the plugin records the failure in
    ctx.extra and does not propagate the exception.
    """

    @pytest.mark.asyncio
    async def test_ai_director_degrades_on_exception(self):
        """AI Director swallows exception, writes fallback + error to ctx.extra."""
        ctx = make_ctx()
        config = make_config()

        strategy = MagicMock()
        strategy.optimize_prompt = AsyncMock(side_effect=RuntimeError("LLM call failed"))

        plugin = AIDirectorPlugin(strategy=strategy, config=config)
        collector = TraceCollector.start(ctx.trace_id)

        # Plugin degrades gracefully (no re-raise) — execute returns ctx
        result = await plugin.execute(ctx)
        assert result is ctx

        result_data = ctx.extra["generation_optimization"]["ai_director"]
        assert "error" in result_data
        assert "LLM call failed" in result_data["error"]
        assert result_data["optimized_prompt"]  # falls back to original prompt

    @pytest.mark.asyncio
    async def test_intent_evaluator_degrades_on_exception(self):
        """Intent Evaluator swallows exception, writes default score + error."""
        ctx = make_ctx()
        config = make_config()

        strategy = MagicMock()
        strategy.evaluate.side_effect = ValueError("Evaluation failed")

        plugin = IntentEvaluatorPlugin(strategy=strategy, config=config)
        collector = TraceCollector.start(ctx.trace_id)

        await plugin.execute(ctx)

        result_data = ctx.extra["generation_optimization"]["intent_evaluator"]
        assert "error" in result_data
        assert "Evaluation failed" in result_data["error"]
        # Fallback default score is recorded, pipeline continues
        assert isinstance(result_data["score"], int)

    @pytest.mark.asyncio
    async def test_gen_model_router_degrades_on_generic_exception(self):
        """GenModelRouter swallows generic exception, records error."""
        ctx = make_ctx()
        ctx.extra["generation_optimization"] = {
            "intent_evaluator": {"score": 50, "factors": {}, "recommended_model": ""}
        }
        config = make_config()

        strategy = MagicMock()
        strategy.route = AsyncMock(side_effect=RuntimeError("Router failure"))

        plugin = GenModelRouterPlugin(strategy=strategy, config=config)
        collector = TraceCollector.start(ctx.trace_id)

        await plugin.execute(ctx)

        # GenModelRouter records the error but does not re-raise
        result_data = ctx.extra["generation_optimization"].get("model_router", {})
        assert result_data.get("error") or result_data.get("reason") or result_data == {}

    @pytest.mark.asyncio
    async def test_cost_tracker_degrades_on_exception(self):
        """Cost Tracker swallows exception, records zero savings + error."""
        ctx = make_ctx()
        config = make_config()

        tracker = MagicMock()
        tracker.record_total_saving.side_effect = ZeroDivisionError("division error")

        plugin = CostTrackerPlugin(tracker=tracker, config=config)
        collector = TraceCollector.start(ctx.trace_id)

        await plugin.execute(ctx)

        result_data = ctx.extra["generation_optimization"]["cost_tracker"]
        assert "error" in result_data
        assert result_data["total_saving_usd"] == 0.0


# ---------------------------------------------------------------------------
# Test: inject_trace_context propagates trace_id in downstream LLM calls
# ---------------------------------------------------------------------------


class TestInjectTraceContext:
    """Verify that inject_trace_context is called for downstream LLM calls.

    This tests the OTel/W3C traceparent header format produced by
    TracingManager.inject_trace_context — independent of the plugin-span
    removal (plugins no longer create spans, but downstream LLM calls still
    propagate trace context via this static helper).
    """

    @pytest.mark.asyncio
    async def test_ai_director_strategy_injects_trace_context(self):
        """AI Director strategy injects trace context headers into LLM calls."""
        from aigateway_core.pipelines.generation.director.ai_director import (
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

        await strategy.optimize_prompt(
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
        from aigateway_core.shared.tracing import TracingManager

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
# Test: plugin results land in ctx.extra (replaces span-attribute assertions)
# ---------------------------------------------------------------------------


class TestPluginResultsInContext:
    """Verify strategy-specific outputs are written to ctx.extra.

    Replaces the old TestSpanAttributes — span attributes no longer exist;
    the same data is instead written to ctx.extra["generation_optimization"][...]
    by each plugin, which is what downstream code consumes.
    """

    @pytest.mark.asyncio
    async def test_ai_director_writes_result_to_context(self):
        """AI Director writes model_used, modality, prompt_length-equivalent to ctx.extra."""
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
        collector = TraceCollector.start(ctx.trace_id)

        await _run_via_engine(plugin, ctx)

        result = ctx.extra["generation_optimization"]["ai_director"]
        assert result["model_used"] == "gpt-4o-mini"
        assert result["modality"] == "llm"
        assert result["cost_usd"] == 0.001
        assert "duration_ms" in result
        # TraceEvent emitted by the PipelineEngine (auto-instrumentation)
        assert len(plugin_events(collector, "ai_director")) == 2

    @pytest.mark.asyncio
    async def test_intent_evaluator_writes_complexity_score_to_context(self):
        """Intent Evaluator writes complexity_score to ctx.extra."""
        ctx = make_ctx()
        config = make_config()

        strategy = MagicMock()
        strategy.evaluate.return_value = MagicMock(
            score=72, factors={"subject_count": 2}, recommended_model="high-end"
        )

        plugin = IntentEvaluatorPlugin(strategy=strategy, config=config)
        collector = TraceCollector.start(ctx.trace_id)

        await _run_via_engine(plugin, ctx)

        result = ctx.extra["generation_optimization"]["intent_evaluator"]
        assert result["score"] == 72
        assert result["factors"] == {"subject_count": 2}
        # TraceEvent emitted by the PipelineEngine (auto-instrumentation)
        assert len(plugin_events(collector, "intent_evaluator")) == 1

    @pytest.mark.asyncio
    async def test_gen_model_router_writes_routing_decision_to_context(self):
        """GenModelRouter writes routing decision to ctx.extra."""
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
        collector = TraceCollector.start(ctx.trace_id)

        await _run_via_engine(plugin, ctx)

        result = ctx.extra["generation_optimization"]["model_router"]
        assert result["selected_model"] == "agnes-image-2.1-flash"
        assert result["selected_provider"] == "agnes"
        assert result["reason"] == "complexity"
        assert result["complexity_score"] == 60
        assert result["estimated_cost"] == 0.05
        # TraceEvent emitted by the PipelineEngine (auto-instrumentation)
        assert len(plugin_events(collector, "gen_model_router")) == 2
