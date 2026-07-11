"""Simulate streaming from a cached response.

Moved verbatim from ``aigateway_api.streaming.simulate_stream_from_cache``
(Task 5). Splits a cached complete response into chunks with a per-chunk
delay to mimic real LLM streaming, per API_CONTRACT.md cache-hit behavior.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, AsyncIterator, Dict


async def simulate_stream_from_cache(
    response_json: str,
    chunk_delay_ms: int = 20,
    chunk_count: int = 20,
    hit_tier: str = "L1",
) -> AsyncIterator[Dict[str, Any]]:
    """将缓存的完整响应按 chunk 分块，模拟流式生成。

    API_CONTRACT.md 缓存命中流式响应特殊行为:
    - 以 20ms/chunk 的延迟模拟真实 LLM 生成
    - 首个 chunk 的 delta.role 为 "assistant"
    - 最后一个 chunk 的 finish_reason 为 "stop"

    Args:
        response_json: 完整的 OpenAI 格式响应 JSON 字符串。
        chunk_delay_ms: 每个 chunk 之间的延迟（毫秒），默认 20。
        chunk_count: 将内容分成多少个 chunk，默认 20。

    Yields:
        模拟的流式 chunk。
    """
    try:
        response_data = json.loads(response_json)
    except json.JSONDecodeError:
        logger.error("缓存命中流式: 响应非合法 JSON，丢弃缓存条目")
        yield {"error": {"code": "internal_error", "message": "Corrupted cache entry"}}
        return

    data = response_data.get("data", response_data)
    choices = data.get("choices", [])
    if not choices:
        yield {"error": {"code": "internal_error", "message": "Empty response"}}
        return

    first_choice = choices[0]
    message = first_choice.get("message", {})
    content = message.get("content", "")
    model = data.get("model", "")
    created = data.get("created", int(time.time()))
    usage = data.get("usage", {})

    # 将内容分成小块
    chunk_size = max(1, len(content) // chunk_count)
    chunks = [content[i:i + chunk_size] for i in range(0, len(content), chunk_size)]

    # 使用统一的 chat_id 以符合 OpenAI SSE 格式（所有 chunk 共享同一 id）
    consistent_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    for i, chunk_text in enumerate(chunks):
        is_last = (i == len(chunks) - 1)

        chunk_data: Dict[str, Any] = {
            "id": consistent_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant" if i == 0 else None,
                    "content": chunk_text if chunk_text else None,
                },
                "finish_reason": None,
            }],
        }

        if is_last:
            chunk_data["choices"][0]["finish_reason"] = "stop"
            if usage:
                chunk_data["usage"] = usage
            # 缓存命中流式：在最后一个 chunk 补充 _meta
            chunk_data["_meta"] = {
                "cache_hit": True,
                "cache_tier": hit_tier,
                "routed_to": {
                    "provider": "cache",
                    "model": model,
                    "tier": hit_tier,
                },
            }

        yield chunk_data
        await asyncio.sleep(chunk_delay_ms / 1000.0)

    yield {"done": True}
