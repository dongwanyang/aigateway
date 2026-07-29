"""TraceEvent + TraceCollector 单元测试."""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.shared.trace_event import (
    TraceCollector,
    TraceEvent,
    append_trace_event,
)


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.ttls = {}

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    async def expire(self, key, ttl):
        self.ttls[key] = ttl


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
    import pytest
    from aigateway_core.dispatch.context import PipelineContext
    with pytest.raises(TypeError):
        PipelineContext(request={"messages": [], "model": "gpt"})  # 缺 trace_id


def test_pipeline_context_with_trace_id():
    from aigateway_core.dispatch.context import PipelineContext
    ctx = PipelineContext(request={"messages": [], "model": "gpt"}, trace_id="t-fixed")
    assert ctx.trace_id == "t-fixed"


@pytest.mark.asyncio
async def test_flush_merges_background_trace_events():
    redis = FakeRedis()
    TraceCollector._current.set(None)
    collector = TraceCollector.start("trace-async")
    collector.emit(
        TraceEvent(
            trace_id="trace-async",
            ts=1.0,
            stage="draft",
            kind="stage",
            name="draft.pending_confirmation",
            duration_ms=1.0,
            status="ok",
        )
    )

    await append_trace_event(
        redis,
        trace_id="trace-async",
        stage="comfyui",
        name="comfyui.workflow_submitted",
        payload={"draft_id": "draft-1"},
    )
    await collector.flush(redis)

    data = json.loads(redis.hashes["aigateway:trace:trace-async"]["data"])
    names = [event["name"] for event in data["events"]]
    assert "draft.pending_confirmation" in names
    assert "comfyui.workflow_submitted" in names
