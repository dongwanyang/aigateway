"""TraceEvent + TraceCollector 单元测试."""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.shared.trace_event import TraceEvent, TraceCollector


def test_trace_event_fields():
    ev = TraceEvent(
        trace_id="t1", ts=time.monotonic(), stage="cache",
        kind="stage", name="prompt_cache.lookup",
        duration_ms=1.5, status="ok",
    )
    assert ev.payload is None
    assert ev.status == "ok"


def test_collector_start_sets_current():
    TraceCollector._current.set(None)  # reset
    c = TraceCollector.start("trace-abc")
    assert c.trace_id == "trace-abc"
    assert TraceCollector.current() is c


def test_collector_emit_accumulates():
    TraceCollector._current.set(None)
    c = TraceCollector.start("trace-abc")
    ev = TraceEvent(trace_id="t1", ts=0.0, stage="auth", kind="stage",
                    name="auth.verify", duration_ms=1.0, status="ok")
    c.emit(ev)
    assert len(c.events) == 1
    assert c.events[0].name == "auth.verify"


def test_collector_current_none_when_not_started():
    TraceCollector._current.set(None)
    assert TraceCollector.current() is None


def test_pipeline_context_trace_id_required():
    """trace_id 不再有默认值,必须显式传入."""
    from aigateway_core.dispatch.context import PipelineContext
    import pytest
    with pytest.raises(TypeError):
        PipelineContext(request={"messages": [], "model": "gpt"})  # 缺 trace_id


def test_pipeline_context_with_trace_id():
    from aigateway_core.dispatch.context import PipelineContext
    ctx = PipelineContext(request={"messages": [], "model": "gpt"}, trace_id="t-fixed")
    assert ctx.trace_id == "t-fixed"
