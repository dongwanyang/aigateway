"""Preserve PR #47's lightweight boolean cancellation contract.

Fully initialized production strategies keep the richer cancellation-record,
confirmation-task and task-tracker flow, including its ``DraftResult`` return.
Lightweight/legacy strategy instances that implement the PR #47 surface use the
local boolean fallback below.
"""
from __future__ import annotations

import asyncio
import functools
import time
from typing import Any

from aigateway_core.pipelines.generation._common.models import (
    DRAFT_STATUS_CANCELLED,
)

from . import draft_generator as _draft_module

_ORIGINAL_ATTR = "_aigateway_original_cancel_draft_pr47"
_WRAPPER_ATTR = "_aigateway_pr47_cancel_contract"


def _is_full_strategy(strategy: Any) -> bool:
    return isinstance(getattr(strategy, "_draft_tasks", None), dict) and isinstance(
        getattr(strategy, "_confirmation_tasks", None), dict
    )


async def _cancel_owned_tasks(strategy: Any, draft_id: str) -> None:
    active_names = {
        f"draft-generate-{draft_id}",
        f"draft-regenerate-{draft_id}",
        f"draft-confirm-{draft_id}",
    }
    tasks = {
        task
        for task in set(getattr(strategy, "_bg_tasks", set()))
        if not task.done() and task.get_name() in active_names
    }
    for task in tasks:
        if task is not asyncio.current_task():
            task.cancel()
    if tasks:
        await asyncio.gather(
            *(task for task in tasks if task is not asyncio.current_task()),
            return_exceptions=True,
        )


async def _cancel_prompt(strategy: Any, draft: Any) -> None:
    prompt_id = str(getattr(draft, "comfy_prompt_id", "") or "")
    if not prompt_id:
        return
    cancel = getattr(strategy, "_cancel_comfyui_workflow", None)
    if not callable(cancel):
        cancel = getattr(strategy, "_cancel_comfy_prompt", None)
    if not callable(cancel):
        return
    try:
        await cancel(prompt_id, server_url=None)
    except TypeError:
        await cancel(prompt_id)


async def _cancel_legacy_strategy(strategy: Any, draft_id: str, draft: Any) -> bool:
    await _cancel_owned_tasks(strategy, draft_id)
    await _cancel_prompt(strategy, draft)

    draft.status = DRAFT_STATUS_CANCELLED
    draft.stage = "cancelled"
    draft.progress = 0.0
    draft.error = "draft_cancelled"
    draft.comfy_prompt_id = None
    params = getattr(draft, "generation_params", None)
    if isinstance(params, dict):
        params["cancelled_at"] = time.time()
        params["progress_source"] = "stage"

    ttl_remaining = max(1, int(float(draft.expires_at) - time.time()))
    await strategy._store_draft(draft, ttl_remaining)
    await strategy._emit_draft_trace(
        str((params or {}).get("trace_id") or ""),
        "draft.cancelled",
        payload={"draft_id": draft_id},
    )
    return True


def install_pr47_cancellation_contract() -> None:
    """Install one idempotent capability-sensitive cancellation adapter."""
    strategy_type = _draft_module.DraftGeneratorStrategy
    current = strategy_type.cancel_draft
    if getattr(current, _WRAPPER_ATTR, False):
        return
    if not hasattr(strategy_type, _ORIGINAL_ATTR):
        setattr(strategy_type, _ORIGINAL_ATTR, current)
    original = getattr(strategy_type, _ORIGINAL_ATTR)
    terminal_statuses = set(_draft_module._TERMINAL_DRAFT_STATUSES)

    @functools.wraps(original)
    async def cancel_draft(self: Any, draft_id: str) -> Any:
        draft = await self.get_draft(draft_id)
        if draft is None:
            return False
        if _is_full_strategy(self):
            # Preserve the production contract: the original method performs
            # idempotent cleanup even for an already-cancelled record and returns
            # the persisted draft object used by cancel_request/API callers.
            return await original(self, draft_id)
        if getattr(draft, "status", None) in terminal_statuses:
            return False
        return await _cancel_legacy_strategy(self, draft_id, draft)

    setattr(cancel_draft, _WRAPPER_ATTR, True)
    strategy_type.cancel_draft = cancel_draft


__all__ = ["install_pr47_cancellation_contract"]
