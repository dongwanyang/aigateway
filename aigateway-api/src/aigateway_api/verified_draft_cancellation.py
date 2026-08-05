"""Require observable ComfyUI release before persisting draft cancellation."""
from __future__ import annotations

import asyncio
import functools
import time
from typing import Any, Awaitable, Callable

import httpx

from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation._common.models import (
    DRAFT_STATUS_CONFIRMING,
    DRAFT_STATUS_GENERATING,
    DRAFT_STATUS_QUEUED,
    DRAFT_STATUS_REFINING,
    DRAFT_STATUS_RUNNING,
)
from aigateway_core.pipelines.generation.draft import (
    _draft_generator_impl as _base_impl,
)
from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)

_ORIGINAL_ATTR = "_aigateway_original_cancel_draft"
_WRAPPER_ATTR = "_aigateway_verified_cancel_draft"
_CANCEL_PREFIX = "aigateway:draft:cancel"
_RESTORABLE_STATUSES = {
    DRAFT_STATUS_GENERATING,
    DRAFT_STATUS_QUEUED,
    DRAFT_STATUS_RUNNING,
    DRAFT_STATUS_CONFIRMING,
    DRAFT_STATUS_REFINING,
}


def _queue_ids(queue: Any, key: str) -> set[str]:
    entries = queue.get(key, []) if isinstance(queue, dict) else []
    return {
        str(item[1])
        for item in entries
        if isinstance(item, (list, tuple)) and len(item) > 1
    }


async def _prompt_released(
    strategy: Any,
    prompt_id: str,
    *,
    server_url: str | None,
    timeout_seconds: float,
) -> bool:
    """Return true only after the owned prompt leaves pending/running queues."""
    base_url = (server_url or strategy._server_url()).rstrip("/")
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    request_timeout = max(
        0.5,
        min(
            2.0,
            float(getattr(strategy._comfyui_config, "connect_timeout", 2.0)),
        ),
    )
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient(timeout=request_timeout) as client:
                response = await client.get(f"{base_url}/queue")
            if response.status_code == 200:
                queue = response.json()
                active = _queue_ids(queue, "queue_pending") | _queue_ids(
                    queue, "queue_running"
                )
                if prompt_id not in active:
                    return True
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            _base_impl.logger.warning(
                "ComfyUI cancellation verification failed",
                extra={
                    "prompt_id": prompt_id,
                    "error_type": type(exc).__name__,
                },
            )
        await asyncio.sleep(0.25)
    return False


def _worker(strategy: Any, draft: Any) -> Any | None:
    coordinator = getattr(strategy, "_gpu_coordinator", None)
    get_worker = getattr(coordinator, "get_worker", None)
    return (
        get_worker(draft.worker_id)
        if callable(get_worker) and getattr(draft, "worker_id", None)
        else None
    )


def _worker_url(strategy: Any, draft: Any) -> str | None:
    worker = _worker(strategy, draft)
    return str(worker.server_url) if worker is not None else None


async def _fence_busy_worker(strategy: Any, draft: Any) -> None:
    """Prevent a still-running prompt's worker from accepting another job."""
    coordinator = getattr(strategy, "_gpu_coordinator", None)
    worker = _worker(strategy, draft)
    if coordinator is None or worker is None:
        return
    condition = getattr(coordinator, "_condition", None)
    if condition is not None:
        async with condition:
            worker.queue_running = max(
                1,
                int(getattr(worker, "queue_running", 0) or 0),
            )
            condition.notify_all()
    else:
        worker.queue_running = max(
            1,
            int(getattr(worker, "queue_running", 0) or 0),
        )
    record_event = getattr(coordinator, "record_event", None)
    if callable(record_event):
        record_event(
            "cancellation_unconfirmed",
            worker_id=str(getattr(worker, "worker_id", "") or ""),
            device_uuid=str(getattr(worker, "device_uuid", "") or ""),
        )


async def _clear_cancel_tombstone(strategy: Any, request_id: str) -> None:
    if not request_id:
        return
    key = f"{_CANCEL_PREFIX}:{request_id}"
    connection = strategy._redis_connection()
    if connection is not None:
        await connection.delete(key)
    else:
        strategy._memory_store.pop(key, None)


def _restore_status(draft: Any) -> str:
    status = str(getattr(draft, "status", "") or "")
    if status in _RESTORABLE_STATUSES:
        return status
    return DRAFT_STATUS_RUNNING


async def _restore_unconfirmed_cancellation(
    strategy: Any,
    draft: Any,
    *,
    prompt_id: str,
    request_id: str,
) -> Any:
    """Restore a recoverable in-progress record after upstream ambiguity."""
    await _clear_cancel_tombstone(strategy, request_id)
    await _fence_busy_worker(strategy, draft)
    current = await strategy.get_draft(draft.draft_id) or draft
    current.status = _restore_status(draft)
    current.stage = "cancellation_unconfirmed"
    current.progress = float(getattr(draft, "progress", 0.0) or 0.0)
    current.error = "comfyui_cancellation_unconfirmed"
    current.comfy_prompt_id = prompt_id
    current.generation_params["cancellation_unconfirmed_at"] = time.time()
    current.generation_params["last_cancel_error"] = (
        "comfyui_cancellation_unconfirmed"
    )
    current.generation_params["progress_source"] = "comfyui"
    ttl_remaining = max(1, int(current.expires_at - time.time()))
    # Bypass the subclass tombstone guard: the tombstone was cleared above and
    # the prompt binding must remain available for runtime reconciliation.
    await _base_impl.DraftGeneratorStrategy._store_draft(
        strategy,
        current,
        ttl_remaining,
    )
    tracker = getattr(strategy, "_task_tracker", None)
    if tracker is not None:
        try:
            await tracker.update_status(
                "draft",
                current.draft_id,
                "running",
                metadata={
                    "request_id": request_id,
                    "error": "comfyui_cancellation_unconfirmed",
                },
            )
        except Exception as exc:
            _base_impl.logger.debug(
                "TaskTracker cancellation-unconfirmed update failed: %s",
                exc,
            )
    await strategy._emit_draft_trace(
        str(current.generation_params.get("trace_id") or ""),
        "draft.cancellation_unconfirmed",
        status="error",
        payload={
            "draft_id": current.draft_id,
            "request_id": request_id,
            "prompt_id": prompt_id,
        },
    )
    return current


async def cancel_draft_verified(
    strategy: Any,
    draft_id: str,
    original: Callable[[Any, str], Awaitable[Any]],
) -> Any:
    """Run cancellation and fail closed if ComfyUI release is unobservable."""
    before = await strategy.get_draft(draft_id)
    if before is None:
        return await original(strategy, draft_id)
    prompt_id = str(getattr(before, "comfy_prompt_id", "") or "")
    request_id = str(before.generation_params.get("request_id") or "")
    server_url = _worker_url(strategy, before)

    result = await original(strategy, draft_id)
    if not prompt_id:
        return result

    configured_timeout = float(
        getattr(strategy._comfyui_config, "connect_timeout", 5.0) or 5.0
    )
    released = await _prompt_released(
        strategy,
        prompt_id,
        server_url=server_url,
        timeout_seconds=max(5.0, min(15.0, configured_timeout * 2.0)),
    )
    if released:
        return result

    await _restore_unconfirmed_cancellation(
        strategy,
        before,
        prompt_id=prompt_id,
        request_id=request_id,
    )
    raise DraftWorkflowError("comfyui_cancellation_unconfirmed")


def install_verified_draft_cancellation() -> None:
    """Install one idempotent verification wrapper on draft cancellation."""
    current = DraftGeneratorStrategy.cancel_draft
    if getattr(current, _WRAPPER_ATTR, False):
        return
    if not hasattr(DraftGeneratorStrategy, _ORIGINAL_ATTR):
        setattr(DraftGeneratorStrategy, _ORIGINAL_ATTR, current)
    original = getattr(DraftGeneratorStrategy, _ORIGINAL_ATTR)

    @functools.wraps(original)
    async def verified(self: Any, draft_id: str) -> Any:
        return await cancel_draft_verified(self, draft_id, original)

    setattr(verified, _WRAPPER_ATTR, True)
    DraftGeneratorStrategy.cancel_draft = verified


__all__ = [
    "cancel_draft_verified",
    "install_verified_draft_cancellation",
]
