"""Stream metrics wrapper.

Moved from ``aigateway_api.openai_compat._wrap_stream_for_metrics``. Wraps a
streaming completion generator, extracts usage from the final chunk, and records
token/cost metrics.
"""
from __future__ import annotations

from typing import Any

from aigateway_core.route.metrics.costing import estimate_model_cost


async def _wrap_stream_for_metrics(
    completion_gen: Any,
    metrics_collector: Any,
    model: str,
    user_id: str = "",
    group_id: str = "",
) -> Any:
    """Pass through chunks and record usage from the final chunk."""
    last_chunk: dict[str, Any] = {}
    async for chunk in completion_gen:
        last_chunk = chunk
        yield chunk

    usage = last_chunk.get("usage", {})
    if not usage:
        return

    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)

    if prompt_tokens > 0:
        metrics_collector.record_tokens(prompt_tokens, "prompt")
    if completion_tokens > 0:
        metrics_collector.record_tokens(completion_tokens, "completion")

    estimate = estimate_model_cost(model, prompt_tokens, completion_tokens)
    if estimate.amount_usd is not None and estimate.amount_usd > 0:
        metrics_collector.record_cost(
            estimate.amount_usd,
            model=model,
            user_id=user_id,
            group_id=group_id,
        )
