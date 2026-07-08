"""
SSE Streaming -- FastAPI 流式响应适配器
=====================================

本模块只保留 FastAPI ``StreamingResponse`` 适配器。SSE 生成器
(``SSEGenerator``) 和缓存命中流式模拟 (``simulate_stream_from_cache``)
已移至 ``aigateway_core.route.streaming`` (Task 5 runtime-structure
refactor)，使核心 dispatch 层可直接 import 而不依赖 API surface。

缓存命中特殊行为（API_CONTRACT.md）由
``aigateway_core.route.streaming.cache_stream.simulate_stream_from_cache``
实现：将缓存的完整响应按 chunk 分块，以 20ms/chunk 延迟模拟真实 LLM 生成。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict

from fastapi.responses import StreamingResponse

from aigateway_core.route.streaming.sse import SSEGenerator


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
