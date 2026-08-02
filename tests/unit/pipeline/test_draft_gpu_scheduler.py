from __future__ import annotations

import time

import pytest
from aigateway_core.pipelines.generation._common.config import DraftWorkflowConfig
from aigateway_core.pipelines.generation._common.models import DraftResult
from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)
from aigateway_core.shared.gpu_scheduler import (
    ComfyWorker,
    GpuDevice,
    GpuResourceCoordinator,
    GpuSchedulerConfig,
)
from aigateway_core.shared.integration_configs import ComfyUIConfig


def _runtime(tmp_path):
    strategy = DraftGeneratorStrategy(
        DraftWorkflowConfig(store_dir=str(tmp_path)),
        comfyui_config=ComfyUIConfig(workflow_version="test"),
        store_dir=str(tmp_path),
    )
    coordinator = GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping(
            {
                "device_safety_margin_gb": 0,
                "comfyui_idle_reservation_seconds": 0,
                "max_worker_failover_attempts": 1,
            }
        ),
        devices=[
            GpuDevice("GPU-a", 0, free_memory_gb=20, total_memory_gb=24),
            GpuDevice("GPU-b", 1, free_memory_gb=20, total_memory_gb=24),
        ],
        workers=[
            ComfyWorker("worker-a", "GPU-a", "http://a"),
            ComfyWorker("worker-b", "GPU-b", "http://b"),
        ],
    )
    strategy._gpu_coordinator = coordinator
    return strategy, coordinator


async def _stored_draft(strategy: DraftGeneratorStrategy) -> DraftResult:
    draft = DraftResult(
        draft_id="draft-test",
        previews=[],
        generation_params={},
        created_at=time.time(),
        expires_at=time.time() + 60,
    )
    await strategy._store_draft(draft, 60)
    return draft


@pytest.mark.asyncio
async def test_explicit_oom_quarantines_worker_and_fails_over_once(tmp_path) -> None:
    strategy, coordinator = _runtime(tmp_path)
    await _stored_draft(strategy)
    calls: list[str] = []

    async def operation() -> bytes:
        url = strategy._server_url()
        calls.append(url)
        if url == "http://a":
            raise RuntimeError("CUDA out of memory")
        return b"result"

    result, worker = await strategy._run_on_comfy_worker(
        "draft-test", "image", operation
    )
    assert result == b"result"
    assert calls == ["http://a", "http://b"]
    assert worker.worker_id == "worker-b"
    restored = await strategy.get_draft("draft-test")
    assert restored is not None
    assert restored.worker_id == "worker-b"
    assert restored.device_uuid == "GPU-b"
    assert coordinator.status()["workers"][0]["oom_quarantine_remaining_seconds"] > 0
    await coordinator.close()


@pytest.mark.asyncio
async def test_accepted_prompt_transport_failure_is_not_blindly_resubmitted(tmp_path) -> None:
    strategy, coordinator = _runtime(tmp_path)
    await _stored_draft(strategy)
    calls = 0

    async def operation() -> bytes:
        nonlocal calls
        calls += 1
        draft = await strategy.get_draft("draft-test")
        assert draft is not None
        draft.comfy_prompt_id = "accepted-prompt"
        await strategy._store_draft(draft, 60)
        raise RuntimeError("connection reset after submit")

    with pytest.raises(RuntimeError, match="connection reset"):
        await strategy._run_on_comfy_worker("draft-test", "image", operation)
    assert calls == 1
    await coordinator.close()
