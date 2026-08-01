from __future__ import annotations

import asyncio
import threading

import pytest
from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.dispatch.pipeline_engine import execute_plugin


class _BlockingPlugin:
    name = "blocking"
    enabled = True
    depends_on: list[str] = []
    pipeline_kind = "understanding"
    timeout_seconds = 0.01

    def __init__(self, release: threading.Event, started: threading.Event) -> None:
        self.release = release
        self.started = started

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        def blocking() -> None:
            self.started.set()
            self.release.wait(timeout=2)

        await asyncio.to_thread(blocking)
        return ctx


@pytest.mark.asyncio
async def test_timeout_waits_for_underlying_thread_before_returning() -> None:
    release = threading.Event()
    started = threading.Event()
    plugin = _BlockingPlugin(release, started)
    ctx = PipelineContext(request={}, trace_id="timeout-thread")
    task = asyncio.create_task(execute_plugin(plugin, ctx))

    while not started.is_set():
        await asyncio.sleep(0)
    await asyncio.sleep(0.03)
    assert not task.done()

    release.set()
    with pytest.raises(TimeoutError):
        await task


@pytest.mark.asyncio
async def test_cancellation_waits_for_underlying_thread_before_returning() -> None:
    release = threading.Event()
    started = threading.Event()
    plugin = _BlockingPlugin(release, started)
    plugin.timeout_seconds = 2
    ctx = PipelineContext(request={}, trace_id="cancel-thread")
    task = asyncio.create_task(execute_plugin(plugin, ctx))

    while not started.is_set():
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0.03)
    assert not task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
