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


class _HeartbeatOutageRedis:
    def __init__(self) -> None:
        self.gateway_claimed = False
        self.generation_claimed = False

    async def eval(self, script: str, _numkeys: int, *args: object) -> int:
        if "smembers" in script:
            self.generation_claimed = True
            return 1
        if "exists" in script and "sadd" in script:
            self.gateway_claimed = True
            return 1
        if "return redis.call('expire'" in script or (
            "sadd" in script and "redis.call('get'" in script
        ):
            raise ConnectionError("redis unavailable")
        return 1


@pytest.mark.asyncio
async def test_gateway_lease_fences_owner_before_redis_ttl_expires() -> None:
    redis = _HeartbeatOutageRedis()
    coordinator = GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping(
            {"lease_ttl_seconds": 0.06, "lease_heartbeat_seconds": 0.01}
        ),
        devices=[GpuDevice("GPU-a", 0, free_memory_gb=20)],
        redis=redis,
    )
    entered = asyncio.Event()

    async def hold() -> None:
        async with coordinator.gateway_lease("embedding", "cuda"):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold())
    await asyncio.wait_for(entered.wait(), timeout=1)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert redis.gateway_claimed is True
    assert coordinator.status()["devices"][0]["gateway_leases"] == 0
    await coordinator.close()


@pytest.mark.asyncio
async def test_generation_lease_fences_owner_during_redis_outage() -> None:
    redis = _HeartbeatOutageRedis()
    coordinator = GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping(
            {
                "lease_ttl_seconds": 0.06,
                "lease_heartbeat_seconds": 0.01,
                "device_safety_margin_gb": 0,
                "comfyui_idle_reservation_seconds": 0,
            }
        ),
        devices=[GpuDevice("GPU-a", 0, free_memory_gb=20)],
        workers=[ComfyWorker("worker-a", "GPU-a", "http://worker")],
        redis=redis,
    )
    entered = asyncio.Event()

    async def hold() -> None:
        async with coordinator.generation_lease("image"):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold())
    await asyncio.wait_for(entered.wait(), timeout=1)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert redis.generation_claimed is True
    assert coordinator.status()["devices"][0]["state"] != "generation_active"
    await coordinator.close()


class _ForeignDrainRedis:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def eval(self, script: str, _numkeys: int, *args: object) -> int:
        self.calls.append(script)
        if "comfyui_release:" in " ".join(str(item) for item in args):
            return [0, 0]
        return 1

    async def delete(self, *_args: object) -> None:
        raise AssertionError("idle release must never issue an unconditional DEL")


@pytest.mark.asyncio
async def test_idle_worker_release_does_not_touch_foreign_drain_owner() -> None:
    redis = _ForeignDrainRedis()
    device = GpuDevice("GPU-a", 0, free_memory_gb=20, comfy_resident=True)
    worker = ComfyWorker("worker-a", "GPU-a", "http://worker")
    coordinator = GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping({}),
        devices=[device],
        workers=[worker],
        redis=redis,
    )
    released: list[str] = []

    async def release(candidate: ComfyWorker) -> bool:
        released.append(candidate.worker_id)
        return True

    coordinator.set_worker_release_hook(release)
    result = await coordinator.release_idle_workers_now()

    assert result == {"worker-a": False}
    assert released == []
    assert device.comfy_resident is True
    await coordinator.close()


@pytest.mark.asyncio
async def test_disabled_scheduler_preserves_legacy_device_request() -> None:
    coordinator = GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping({"enabled": False}),
        devices=[GpuDevice("GPU-a", 0, free_memory_gb=20)],
    )

    async with coordinator.gateway_lease("embedding", "cuda:0") as lease:
        assert lease.device == "cuda:0"
        assert lease.device_uuid is None

    await coordinator.close()


@pytest.mark.asyncio
async def test_file_fence_blocks_generation_after_redis_lease_visibility_is_lost(
    tmp_path,
) -> None:
    lock_dir = tmp_path / "gpu-locks"
    first = GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping(
            {
                "generation_wait_timeout_seconds": 0.05,
                "device_safety_margin_gb": 0,
            }
        ),
        devices=[GpuDevice("GPU-a", 0, free_memory_gb=20)],
        lock_dir=lock_dir,
    )
    second = GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping(
            {
                "generation_wait_timeout_seconds": 0.05,
                "device_safety_margin_gb": 0,
            }
        ),
        devices=[GpuDevice("GPU-a", 0, free_memory_gb=20)],
        workers=[ComfyWorker("worker-a", "GPU-a", "http://worker")],
        lock_dir=lock_dir,
    )
    lease = first.gateway_lease("embedding", "cuda")
    await lease.__aenter__()

    with pytest.raises(asyncio.TimeoutError):
        async with second.generation_lease("image"):
            pass

    await lease.__aexit__(None, None, None)
    async with second.generation_lease("image") as worker:
        assert worker.worker_id == "worker-a"
    await first.close()
    await second.close()


@pytest.mark.asyncio
async def test_file_fence_blocks_gateway_borrow_during_generation(tmp_path) -> None:
    lock_dir = tmp_path / "gpu-locks"
    generation = GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping(
            {"device_safety_margin_gb": 0, "comfyui_idle_reservation_seconds": 0}
        ),
        devices=[GpuDevice("GPU-a", 0, free_memory_gb=20)],
        workers=[ComfyWorker("worker-a", "GPU-a", "http://worker")],
        lock_dir=lock_dir,
    )
    gateway = GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping({"gateway_fallback": "cpu"}),
        devices=[GpuDevice("GPU-a", 0, free_memory_gb=20)],
        lock_dir=lock_dir,
    )

    async with generation.generation_lease("image"):
        async with gateway.gateway_lease("embedding", "auto") as lease:
            assert lease.device == "cpu"

    await generation.close()
    await gateway.close()


@pytest.mark.asyncio
async def test_initial_worker_probe_blocks_gateway_until_comfy_queue_is_empty(
    tmp_path,
) -> None:
    queue_pending = 1
    coordinator = GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping({"gateway_fallback": "cpu"}),
        devices=[GpuDevice("GPU-a", 0, free_memory_gb=20)],
        workers=[ComfyWorker("worker-a", "GPU-a", "http://worker")],
        lock_dir=tmp_path / "locks",
    )

    async def probe(_worker: ComfyWorker) -> dict[str, object]:
        return {"healthy": True, "running": 0, "pending": queue_pending}

    coordinator.set_worker_probe_hook(probe)
    await coordinator.start()

    async with coordinator.gateway_lease("embedding", "auto") as blocked:
        assert blocked.device == "cpu"

    queue_pending = 0
    await coordinator._probe_all_workers()
    async with coordinator.gateway_lease("embedding", "auto") as available:
        assert available.device_uuid == "GPU-a"

    await coordinator.close()


class _IdleReservationRedis:
    def __init__(self, initial: str | None) -> None:
        self.owner = initial

    async def eval(self, script: str, _numkeys: int, *args: object):
        if "return {1, restore_idle}" in script:
            if self.owner not in {None, "comfyui_idle"}:
                return [0, 0]
            restore = 1 if self.owner == "comfyui_idle" else 0
            self.owner = str(args[1])
            return [1, restore]
        if "ARGV[3] == '0'" in script:
            token = str(args[1])
            if self.owner != token:
                return 0
            released = str(args[2]) == "1"
            restore_idle = str(args[3]) == "1"
            self.owner = None if released or not restore_idle else "comfyui_idle"
            return 1
        return 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_owner", "expected_owner"),
    [(None, None), ("comfyui_idle", "comfyui_idle")],
)
async def test_failed_idle_release_restores_only_a_preexisting_idle_reservation(
    tmp_path, initial_owner: str | None, expected_owner: str | None
) -> None:
    redis = _IdleReservationRedis(initial_owner)
    coordinator = GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping({}),
        devices=[GpuDevice("GPU-a", 0, free_memory_gb=20, comfy_resident=True)],
        workers=[ComfyWorker("worker-a", "GPU-a", "http://worker")],
        redis=redis,
        lock_dir=tmp_path / "locks",
    )
    coordinator.set_worker_release_hook(lambda _worker: False)

    result = await coordinator.release_idle_workers_now()

    assert result == {"worker-a": False}
    assert redis.owner == expected_owner
    assert coordinator._device_file_locks == {}
    await coordinator.close()
