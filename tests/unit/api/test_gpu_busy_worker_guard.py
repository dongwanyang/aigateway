from __future__ import annotations

from aigateway_api.gpu_queue_handoff import install_gpu_queue_handoff
from aigateway_core.shared.gpu_scheduler import (
    ComfyWorker,
    GpuDevice,
    GpuResourceCoordinator,
    GpuSchedulerConfig,
)


def _coordinator(worker: ComfyWorker) -> GpuResourceCoordinator:
    device = GpuDevice(
        uuid="gpu-1",
        logical_index=0,
        total_memory_gb=24.0,
        free_memory_gb=20.0,
    )
    return GpuResourceCoordinator(
        GpuSchedulerConfig(),
        devices=[device],
        workers=[worker],
    )


def test_running_worker_is_not_a_generation_candidate() -> None:
    install_gpu_queue_handoff()
    worker = ComfyWorker(
        worker_id="worker-1",
        device_uuid="gpu-1",
        server_url="http://comfyui:8188",
        queue_running=1,
    )
    coordinator = _coordinator(worker)

    assert coordinator._worker_candidates("video", 8.0, set()) == []


def test_pending_worker_is_not_a_generation_candidate() -> None:
    install_gpu_queue_handoff()
    worker = ComfyWorker(
        worker_id="worker-1",
        device_uuid="gpu-1",
        server_url="http://comfyui:8188",
        queue_pending=1,
    )
    coordinator = _coordinator(worker)

    assert coordinator._worker_candidates("video", 8.0, set()) == []


def test_worker_becomes_candidate_only_after_queue_is_idle() -> None:
    install_gpu_queue_handoff()
    worker = ComfyWorker(
        worker_id="worker-1",
        device_uuid="gpu-1",
        server_url="http://comfyui:8188",
        queue_running=1,
    )
    coordinator = _coordinator(worker)

    worker.queue_running = 0
    worker.queue_pending = 0
    candidates = coordinator._worker_candidates("video", 8.0, set())

    assert len(candidates) == 1
    assert candidates[0][1] is worker
