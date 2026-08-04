from __future__ import annotations

import asyncio

import pytest
from aigateway_core.shared.gpu_scheduler import (
    ComfyWorker,
    GpuDevice,
    GpuLeaseUnavailableError,
    GpuQueueTimeoutError,
    GpuResourceCoordinator,
    GpuSchedulerConfig,
    GpuSchedulerConfigError,
)


class _LeaseRedis:
    def __init__(self) -> None:
        self.heartbeat = asyncio.Event()
        self.eval_calls: list[tuple[str, int, tuple[object, ...]]] = []

    async def eval(
        self, script: str, numkeys: int, *args: object
    ) -> int:
        self.eval_calls.append((script, numkeys, args))
        if numkeys == 2 and "sadd" in script and "expire" in script:
            self.heartbeat.set()
        return 1


class _Metrics:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []
        self.queue_depths: list[int] = []
        self.waits: list[float] = []

    def record_gpu_scheduler_event(
        self, event: str, *, worker_id: str, device_uuid: str
    ) -> None:
        self.events.append((event, worker_id, device_uuid))

    def set_gpu_generation_queue_depth(self, depth: int) -> None:
        self.queue_depths.append(depth)

    def record_gpu_generation_wait(self, duration_seconds: float) -> None:
        self.waits.append(duration_seconds)


def _coordinator(**config: object) -> GpuResourceCoordinator:
    devices = [
        GpuDevice("GPU-a", 0, "small", 16, 14),
        GpuDevice("GPU-b", 1, "large", 48, 44),
    ]
    workers = [
        ComfyWorker("worker-a", "GPU-a", "http://a", frozenset({"image"})),
        ComfyWorker(
            "worker-b", "GPU-b", "http://b", frozenset({"image", "video", "upscale"})
        ),
    ]
    return GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping(config), devices=devices, workers=workers
    )


def test_scheduler_config_validates_heartbeat_and_exposes_defaults() -> None:
    config = GpuSchedulerConfig.from_mapping({})
    assert config.generation_wait_timeout_seconds == 600
    assert config.comfyui_idle_reservation_seconds == 60
    assert config.gateway_memory_limit_percent is None
    assert config.comfyui_dynamic_vram_enabled is False
    with pytest.raises(GpuSchedulerConfigError, match="less than"):
        GpuSchedulerConfig.from_mapping(
            {"lease_ttl_seconds": 5, "lease_heartbeat_seconds": 5}
        )
    with pytest.raises(GpuSchedulerConfigError, match="must be a boolean"):
        GpuSchedulerConfig.from_mapping(
            {"comfyui_dynamic_vram_enabled": "false"}
        )


@pytest.mark.asyncio
async def test_generation_drain_rejects_new_gpu_lease_and_uses_cpu_fallback() -> None:
    coordinator = _coordinator(device_safety_margin_gb=0)
    first_lease = coordinator.gateway_lease("embedding", "cuda:1")
    active = await first_lease.__aenter__()
    assert active.device_uuid == "GPU-b"

    generation_entered = asyncio.Event()

    async def generation() -> None:
        async with coordinator.generation_lease(
            "video", memory_requirement_gb=20
        ) as worker:
            assert worker.worker_id == "worker-b"
            generation_entered.set()

    task = asyncio.create_task(generation())
    for _ in range(100):
        if coordinator.status()["devices"][1]["state"] == "draining":
            break
        await asyncio.sleep(0)
    async with coordinator.gateway_lease("clip", "auto") as fallback:
        assert fallback.device_uuid == "GPU-a"

    await first_lease.__aexit__(None, None, None)
    await asyncio.wait_for(generation_entered.wait(), timeout=1)
    await task
    await coordinator.close()


@pytest.mark.asyncio
async def test_cuda_index_is_strict_and_worker_selection_uses_capability_and_memory() -> None:
    coordinator = _coordinator(device_safety_margin_gb=2)
    async with coordinator.gateway_lease("clip", "cuda:0") as lease:
        assert lease.device_uuid == "GPU-a"
    async with coordinator.generation_lease(
        "video", memory_requirement_gb=30
    ) as worker:
        assert worker.worker_id == "worker-b"
    with pytest.raises(GpuLeaseUnavailableError):
        async with coordinator.gateway_lease("clip", "cuda:9"):
            pass
    await coordinator.close()


@pytest.mark.asyncio
async def test_generation_timeout_is_configurable() -> None:
    coordinator = _coordinator(
        generation_wait_timeout_seconds=0.01,
        device_safety_margin_gb=0,
    )
    lease = coordinator.gateway_lease("embedding", "cuda:1")
    await lease.__aenter__()
    with pytest.raises(GpuQueueTimeoutError):
        async with coordinator.generation_lease("video"):
            pass
    await lease.__aexit__(None, None, None)
    await coordinator.close()


@pytest.mark.asyncio
async def test_generation_reuses_memory_reserved_by_comfy_worker() -> None:
    device = GpuDevice(
        "GPU-a",
        0,
        "resident-model",
        total_memory_gb=16,
        free_memory_gb=7.5,
        worker_reserved_memory_gb=6.5,
    )
    coordinator = GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping(
            {
                "device_safety_margin_gb": 2,
                "generation_wait_timeout_seconds": 0.01,
                "comfyui_idle_reservation_seconds": 0,
            }
        ),
        devices=[device],
        workers=[ComfyWorker("worker-a", "GPU-a", "http://worker")],
    )

    async with coordinator.generation_lease(
        "image", memory_requirement_gb=8
    ) as worker:
        assert worker.worker_id == "worker-a"

    await coordinator.close()


@pytest.mark.asyncio
async def test_oom_quarantine_and_hot_update_preserve_restart_topology() -> None:
    coordinator = _coordinator(
        gateway_devices=["GPU-a"],
        comfyui_devices=["GPU-a", "GPU-b"],
        oom_quarantine_seconds=10,
    )
    await coordinator.quarantine_oom("worker-b")
    with pytest.raises(GpuQueueTimeoutError):
        coordinator.update_hot_config(
            {
                "gateway_devices": ["GPU-b"],
                "comfyui_devices": ["GPU-b"],
                "comfyui_dynamic_vram_enabled": True,
                "generation_wait_timeout_seconds": 0.01,
            }
        )
        async with coordinator.generation_lease("video"):
            pass
    assert coordinator.config.gateway_devices == ("GPU-a",)
    assert coordinator.config.comfyui_dynamic_vram_enabled is False
    assert coordinator.config.generation_wait_timeout_seconds == 0.01
    await coordinator.close()


@pytest.mark.asyncio
async def test_single_gpu_generation_drain_falls_back_to_cpu_and_cancellation_cleans_state() -> None:
    device = GpuDevice("GPU-only", 0, "single", 24, 22)
    coordinator = GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping(
            {"device_safety_margin_gb": 0, "gateway_fallback": "cpu"}
        ),
        devices=[device],
        workers=[ComfyWorker("worker-only", "GPU-only", "http://only")],
    )
    lease = coordinator.gateway_lease("embedding", "cuda:0")
    await lease.__aenter__()
    waiting = asyncio.create_task(
        coordinator.generation_lease("image").__aenter__()
    )
    for _ in range(100):
        if device.draining:
            break
        await asyncio.sleep(0)
    async with coordinator.gateway_lease("clip", "auto") as fallback:
        assert fallback.device == "cpu"
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await asyncio.sleep(0)
    assert device.draining is False
    assert coordinator.status()["generation_queue_depth"] == 0
    await lease.__aexit__(None, None, None)
    await coordinator.close()


@pytest.mark.asyncio
async def test_redis_lease_heartbeat_renews_lease_and_membership_ttls() -> None:
    redis = _LeaseRedis()
    coordinator = GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping(
            {"lease_ttl_seconds": 0.05, "lease_heartbeat_seconds": 0.01}
        ),
        devices=[GpuDevice("GPU-a", 0, free_memory_gb=20)],
        redis=redis,
    )
    async with coordinator.gateway_lease("embedding", "cuda") as lease:
        await asyncio.wait_for(redis.heartbeat.wait(), timeout=1)
        heartbeat_calls = [
            call
            for call in redis.eval_calls
            if call[1] == 2 and "sadd" in call[0] and "expire" in call[0]
        ]
        assert heartbeat_calls
        _, _, args = heartbeat_calls[-1]
        assert args[0].endswith(lease.lease_id)
        assert args[1].endswith("leases:GPU-a")
        assert args[2] == lease.lease_id
    cleanup_calls = [call for call in redis.eval_calls if "srem" in call[0]]
    assert cleanup_calls
    await coordinator.close()


@pytest.mark.asyncio
async def test_scheduler_records_borrow_allocation_queue_wait_and_oom_metrics() -> None:
    metrics = _Metrics()
    coordinator = GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping(
            {
                "device_safety_margin_gb": 0,
                "comfyui_idle_reservation_seconds": 0,
            }
        ),
        devices=[GpuDevice("GPU-a", 0, free_memory_gb=20)],
        workers=[ComfyWorker("worker-a", "GPU-a", "http://worker")],
        metrics_collector=metrics,
    )
    async with coordinator.gateway_lease("embedding", "cuda"):
        pass
    async with coordinator.generation_lease("image") as worker:
        assert worker.worker_id == "worker-a"
    await coordinator.quarantine_oom("worker-a")

    assert ("gateway_borrow", "", "GPU-a") in metrics.events
    assert ("worker_allocation", "worker-a", "GPU-a") in metrics.events
    assert ("oom_quarantine", "worker-a", "GPU-a") in metrics.events
    assert metrics.queue_depths[-1] == 0
    assert len(metrics.waits) == 1
    await coordinator.close()
