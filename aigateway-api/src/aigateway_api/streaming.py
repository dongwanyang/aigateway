"""
SSE Streaming -- 流式响应封装
============================

实现 Server-Sent Events (SSE) 流式响应，用于 /v1/chat/completions?stream=true。

缓存命中时特殊行为（API_CONTRACT.md）：
- 将缓存的完整响应按 chunk 分块，以 20ms/chunk 延迟模拟真实 LLM 生成
- 首个 chunk 的 delta.role 为 "assistant"
- 最后一个 chunk 的 finish_reason 为 "stop"
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)


class SSEGenerator:
    """SSE 流式响应生成器。"""

    def __init__(
        self,
        completion_gen: AsyncIterator[Dict[str, Any]],
        chat_id: str = None,
    ) -> None:
        self.completion_gen = completion_gen
        self.chat_id = chat_id or f"chatcmpl-{uuid.uuid4().hex[:12]}"

    async def generate(self) -> AsyncIterator[str]:
        """生成 SSE 格式的数据流。"""
        try:
            async for chunk in self.completion_gen:
                sse_line = "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
                yield sse_line
        except Exception as exc:
            logger.error("SSE stream generation error: %s", exc)
            error_chunk = {
                "error": {"code": "internal_error", "message": str(exc)},
            }
            yield "data: " + json.dumps(error_chunk, ensure_ascii=False) + "\n\n"
        finally:
            yield "data: [DONE]\n\n"


def create_sse_response(
    completion_gen: AsyncIterator[Dict[str, Any]],
    chat_id: str = None,
) -> StreamingResponse:
    """创建 SSE StreamingResponse。

    Args:
        completion_gen: 下游 LLM 返回的异步迭代器。
        chat_id: 聊天补全请求 ID。

    Returns:
        StreamingResponse，Content-Type 为 text/event-stream。
    """
    generator = SSEGenerator(completion_gen, chat_id)
    return StreamingResponse(
        generator.generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def simulate_stream_from_cache(
    response_json: str,
    chunk_delay_ms: int = 20,
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

    Yields:
        模拟的流式 chunk。
    """
    try:
        response_data = json.loads(response_json)
    except json.JSONDecodeError:
        response_data = {"data": {"choices": [{"message": {"content": response_json}}]}}

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
    chunk_size = max(1, len(content) // 20)
    chunks = [content[i:i + chunk_size] for i in range(0, len(content), chunk_size)]

    for i, chunk_text in enumerate(chunks):
        is_last = (i == len(chunks) - 1)

        chunk_data: Dict[str, Any] = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
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
