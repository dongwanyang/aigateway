from __future__ import annotations

import asyncio
import math

import pytest
from aigateway_core.shared.gpu_scheduler import (
    ComfyWorker,
    GpuDevice,
    GpuResourceCoordinator,
    GpuSchedulerConfig,
    GpuSchedulerConfigError,
)


class _GenerationRedis:
    def __init__(self) -> None:
        self.renewed = asyncio.Event()

    async def eval(self, script: str, _numkeys: int, *args: object) -> int:
        if "smembers" in script:
            drain_key = str(args[1])
            return 0 if drain_key.endswith("GPU-a") else 1
        if "return redis.call('expire'" in script:
            self.renewed.set()
        return 1


@pytest.mark.parametrize("field", ["enabled", "generation_priority"])
def test_boolean_fields_reject_string_false(field: str) -> None:
    with pytest.raises(GpuSchedulerConfigError, match="must be a boolean"):
        GpuSchedulerConfig.from_mapping({field: "false"})


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_numeric_fields_reject_non_finite_values(value: float) -> None:
    with pytest.raises(GpuSchedulerConfigError, match="finite"):
        GpuSchedulerConfig.from_mapping(
            {"generation_wait_timeout_seconds": value}
        )


@pytest.mark.asyncio
async def test_generation_tries_next_candidate_after_distributed_claim_denied() -> None:
    redis = _GenerationRedis()
    coordinator = GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping(
            {
                "device_safety_margin_gb": 0,
                "comfyui_idle_reservation_seconds": 0,
            }
        ),
        devices=[
            GpuDevice("GPU-a", 0, free_memory_gb=20),
            GpuDevice("GPU-b", 1, free_memory_gb=20),
        ],
        workers=[
            ComfyWorker("worker-a", "GPU-a", "http://a"),
            ComfyWorker("worker-b", "GPU-b", "http://b"),
        ],
        redis=redis,
    )

    async with coordinator.generation_lease("image") as worker:
        assert worker.worker_id == "worker-b"

    await coordinator.close()


@pytest.mark.asyncio
async def test_generation_claim_is_renewed_while_work_is_running() -> None:
    redis = _GenerationRedis()
    coordinator = GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping(
            {
                "lease_ttl_seconds": 0.05,
                "lease_heartbeat_seconds": 0.01,
                "device_safety_margin_gb": 0,
                "comfyui_idle_reservation_seconds": 0,
            }
        ),
        devices=[GpuDevice("GPU-b", 0, free_memory_gb=20)],
        workers=[ComfyWorker("worker-b", "GPU-b", "http://b")],
        redis=redis,
    )

    async with coordinator.generation_lease("image"):
        await asyncio.wait_for(redis.renewed.wait(), timeout=1)

    await coordinator.close()


@pytest.mark.asyncio
async def test_idle_loop_skips_queued_workers_and_survives_hook_error() -> None:
    loop = asyncio.get_running_loop()
    devices = [
        GpuDevice("GPU-a", 0, free_memory_gb=20),
        GpuDevice("GPU-b", 1, free_memory_gb=20),
    ]
    for device in devices:
        device.reserved_until = loop.time() + 0.01
        device.comfy_resident = True
    workers = [
        ComfyWorker("worker-a", "GPU-a", "http://a"),
        ComfyWorker("worker-b", "GPU-b", "http://b"),
    ]
    coordinator = GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping(
            {
                "worker_probe_interval_seconds": 0.01,
                "worker_unhealthy_cooldown_seconds": 0,
            }
        ),
        devices=devices,
        workers=workers,
    )
    released: list[str] = []

    async def probe(worker: ComfyWorker) -> dict[str, object]:
        return {
            "healthy": True,
            "running": 0,
            "pending": 1 if worker.worker_id == "worker-a" else 0,
        }

    async def release(worker: ComfyWorker) -> bool:
        if worker.worker_id == "worker-b" and not released:
            released.append("error")
            raise RuntimeError("transient")
        released.append(worker.worker_id)
        return True

    coordinator.set_worker_probe_hook(probe)
    coordinator.set_worker_release_hook(release)
    await coordinator.start()
    await coordinator.start()
    await asyncio.sleep(0.08)

    assert "worker-a" not in released
    assert "worker-b" in released
    assert len(coordinator._background_tasks) == 1
    await coordinator.close()
