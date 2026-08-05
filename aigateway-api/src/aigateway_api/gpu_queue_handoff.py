"""Prevent ComfyUI idle reservation from blocking queued generation work."""
from __future__ import annotations

import functools
from typing import Any

from aigateway_core.shared.gpu_scheduler import GpuResourceCoordinator

_ORIGINAL_ATTR = "_aigateway_original_redis_reserve_after_generation"
_WRAPPER_ATTR = "_aigateway_disable_idle_generation_reservation"


def install_gpu_queue_handoff() -> None:
    """Release the generation drain immediately after each completed job.

    The previous ``comfyui_idle`` owner blocked every queued ticket for the full
    reservation TTL. Until the scheduler has a distributed waiter count, an idle
    reservation is not safe on the single-GPU FIFO path, so the effective
    reservation is forced to zero.
    """
    current = GpuResourceCoordinator._redis_reserve_after_generation
    if getattr(current, _WRAPPER_ATTR, False):
        return
    if not hasattr(GpuResourceCoordinator, _ORIGINAL_ATTR):
        setattr(GpuResourceCoordinator, _ORIGINAL_ATTR, current)
    original = getattr(GpuResourceCoordinator, _ORIGINAL_ATTR)

    @functools.wraps(original)
    async def release_immediately(
        self: Any,
        device_uuid: str,
        ticket: str,
        seconds: float,
    ) -> bool:
        return await original(self, device_uuid, ticket, 0.0)

    setattr(release_immediately, _WRAPPER_ATTR, True)
    GpuResourceCoordinator._redis_reserve_after_generation = release_immediately


__all__ = ["install_gpu_queue_handoff"]
