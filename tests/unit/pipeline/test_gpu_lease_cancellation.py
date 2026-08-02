from __future__ import annotations

import asyncio
import threading

import pytest
from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.dispatch.pipeline_engine import (
    PipelineEngine,
    _ORPHANED_PLUGIN_TASKS,
    execute_plugin,
)
from aigateway_core.shared.gpu_scheduler import (
    GpuDevice,
    GpuResourceCoordinator,
    GpuSchedulerConfig,
)


class _BlockingPlugin:
    name = "blocking"
    enabled = True
    depends_on: list[str] = []
    pipeline_kind = "understanding"
    timeout_seconds = 0.01
    failure_policy = "continue"

    def __init__(self, release: threading.Event, started: threading.Event) -> None:
        self.release = release
        self.started = started

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        def blocking() -> None:
            self.started.set()
            self.release.wait(timeout=2)

        await asyncio.to_thread(blocking)
        ctx.extra["completed"] = True
        return ctx


class _GpuBlockingPlugin(_BlockingPlugin):
    name = "gpu-blocking"
    gpu_device_request = "cuda"

    def __init__(self, release: threading.Event, started: threading.Event) -> None:
        super().__init__(release, started)
        self.runtime_device: str | None = None

    def set_runtime_device(self, device: str) -> None:
        self.runtime_device = device


async def _wait_for_orphans_to_clear() -> None:
    for _ in range(200):
        if not _ORPHANED_PLUGIN_TASKS:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("orphaned plugin task did not finish")


@pytest.mark.asyncio
async def test_timeout_returns_while_underlying_thread_finishes_in_background() -> None:
    release = threading.Event()
    started = threading.Event()
    plugin = _BlockingPlugin(release, started)
    ctx = PipelineContext(request={}, trace_id="timeout-thread")

    with pytest.raises(TimeoutError):
        await execute_plugin(plugin, ctx)

    assert started.is_set()
    assert _ORPHANED_PLUGIN_TASKS
    assert "completed" not in ctx.extra
    release.set()
    await _wait_for_orphans_to_clear()


@pytest.mark.asyncio
async def test_cancellation_returns_while_underlying_thread_finishes_in_background() -> None:
    release = threading.Event()
    started = threading.Event()
    plugin = _BlockingPlugin(release, started)
    plugin.timeout_seconds = 2
    ctx = PipelineContext(request={}, trace_id="cancel-thread")
    task = asyncio.create_task(execute_plugin(plugin, ctx))

    while not started.is_set():
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _ORPHANED_PLUGIN_TASKS

    release.set()
    await _wait_for_orphans_to_clear()


@pytest.mark.asyncio
async def test_timed_out_gpu_plugin_keeps_lease_until_real_work_finishes() -> None:
    release = threading.Event()
    started = threading.Event()
    plugin = _GpuBlockingPlugin(release, started)
    device = GpuDevice("GPU-a", 0, free_memory_gb=16)
    coordinator = GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping({}),
        devices=[device],
    )
    engine = PipelineEngine(registry=None, pipeline_kind="understanding")  # type: ignore[arg-type]
    engine._initialized = True
    engine._ordered_plugins = [plugin]
    engine.gpu_coordinator = coordinator
    original = PipelineContext(request={}, trace_id="gpu-timeout")

    result = await asyncio.wait_for(engine.execute_ctx(original), timeout=0.5)

    assert started.is_set()
    assert plugin.runtime_device == "cuda:0"
    assert device.gateway_leases
    assert "completed" not in result.extra

    release.set()
    await _wait_for_orphans_to_clear()
    for _ in range(100):
        if not device.gateway_leases:
            break
        await asyncio.sleep(0.01)
    assert not device.gateway_leases
    await coordinator.close()
