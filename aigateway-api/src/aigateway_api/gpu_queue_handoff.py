"""Keep single-GPU generation handoff FIFO and free of busy-worker reuse."""
from __future__ import annotations

import functools
from typing import Any

from aigateway_core.shared.gpu_scheduler import GpuResourceCoordinator

_RESERVE_ORIGINAL_ATTR = "_aigateway_original_redis_reserve_after_generation"
_RESERVE_WRAPPER_ATTR = "_aigateway_disable_idle_generation_reservation"
_CANDIDATES_ORIGINAL_ATTR = "_aigateway_original_worker_candidates"
_CANDIDATES_WRAPPER_ATTR = "_aigateway_exclude_busy_workers"


def _install_immediate_drain_release() -> None:
    current = GpuResourceCoordinator._redis_reserve_after_generation
    if getattr(current, _RESERVE_WRAPPER_ATTR, False):
        return
    if not hasattr(GpuResourceCoordinator, _RESERVE_ORIGINAL_ATTR):
        setattr(GpuResourceCoordinator, _RESERVE_ORIGINAL_ATTR, current)
    original = getattr(GpuResourceCoordinator, _RESERVE_ORIGINAL_ATTR)

    @functools.wraps(original)
    async def release_immediately(
        self: Any,
        device_uuid: str,
        ticket: str,
        seconds: float,
    ) -> bool:
        return await original(self, device_uuid, ticket, 0.0)

    setattr(release_immediately, _RESERVE_WRAPPER_ATTR, True)
    GpuResourceCoordinator._redis_reserve_after_generation = release_immediately


def _install_busy_worker_guard() -> None:
    current = GpuResourceCoordinator._worker_candidates
    if getattr(current, _CANDIDATES_WRAPPER_ATTR, False):
        return
    if not hasattr(GpuResourceCoordinator, _CANDIDATES_ORIGINAL_ATTR):
        setattr(GpuResourceCoordinator, _CANDIDATES_ORIGINAL_ATTR, current)
    original = getattr(GpuResourceCoordinator, _CANDIDATES_ORIGINAL_ATTR)

    @functools.wraps(original)
    def idle_worker_candidates(
        self: Any,
        capability: str,
        memory_requirement_gb: float,
        excluded_workers: set[str],
        preferred_worker_id: str | None = None,
    ) -> list[tuple[float, Any, Any]]:
        candidates = original(
            self,
            capability,
            memory_requirement_gb,
            excluded_workers,
            preferred_worker_id,
        )
        # A score penalty is insufficient on a one-worker deployment: the busy
        # worker remains the only candidate and receives another prompt after a
        # cancelled local Task releases its semaphore/lease. Treat the observed
        # ComfyUI queue as a hard ownership fence instead.
        return [
            candidate
            for candidate in candidates
            if int(getattr(candidate[1], "queue_running", 0) or 0) == 0
            and int(getattr(candidate[1], "queue_pending", 0) or 0) == 0
        ]

    setattr(idle_worker_candidates, _CANDIDATES_WRAPPER_ATTR, True)
    GpuResourceCoordinator._worker_candidates = idle_worker_candidates


def install_gpu_queue_handoff() -> None:
    """Install safe single-GPU handoff behavior.

    Idle reservation previously blocked every queued ticket for the full TTL,
    while a worker with a still-running ComfyUI prompt could remain selectable
    after its local owning Task was cancelled. Release the drain immediately and
    require an observably idle worker before allocating new generation work.
    """
    _install_immediate_drain_release()
    _install_busy_worker_guard()


__all__ = ["install_gpu_queue_handoff"]
