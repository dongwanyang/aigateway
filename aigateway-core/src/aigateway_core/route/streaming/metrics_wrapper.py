"""Stream metrics wrapper.

Moved verbatim from ``aigateway_api.openai_compat._wrap_stream_for_metrics``
(Task 5). Wraps a streaming completion generator, extracts usage from the
final chunk, and records token/cost metrics.

Note: the core dispatcher (``dispatch.dispatcher._wrap_stream_full``) has
since inlined a richer version of this logic (it also does quota deduction
+ cache backfill). This wrapper is retained for parity and any future
caller that only needs the metrics-recording subset.
"""
from __future__ import annotations

from typing import Any, Dict

from aigateway_core.route.metrics.costing import _estimate_cost


async def _wrap_stream_for_metrics(
    completion_gen: Any,
    metrics_collector: Any,
    model: str,
    user_id: str = "",
    group_id: str = "",
) -> Any:
    """包装流式生成器，从最后一个 chunk 提取 usage 并记录指标。"""
    last_chunk: Dict[str, Any] = {}
    async for chunk in completion_gen:
        last_chunk = chunk
        yield chunk

    # 从最后一个 chunk 提取 usage 数据
    usage = last_chunk.get("usage", {})
    if not usage:
        return

    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    total_tokens = usage.get("total_tokens", 0)

    if prompt_tokens > 0:
        metrics_collector.record_tokens(prompt_tokens, "prompt")
    if completion_tokens > 0:
        metrics_collector.record_tokens(completion_tokens, "completion")
    if total_tokens > 0:
        cost = _estimate_cost(model, total_tokens)
        if cost > 0:
            metrics_collector.record_cost(cost, model=model, user_id=user_id, group_id=group_id)
