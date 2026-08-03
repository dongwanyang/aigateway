"""SSE format generator.

Moved verbatim from ``aigateway_api.streaming.SSEGenerator`` (Task 5).
The FastAPI ``StreamingResponse`` adapter that wraps this generator
remains in ``aigateway_api.streaming.create_sse_response``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger(__name__)


class SSEGenerator:
    """SSE 流式响应生成器。"""

    def __init__(
        self,
        completion_gen: AsyncIterator[dict[str, Any]],
        chat_id: str | None = None,
    ) -> None:
        self.completion_gen = completion_gen
        self.chat_id = chat_id or f"chatcmpl-{uuid.uuid4().hex[:12]}"

    async def generate(self) -> AsyncIterator[str]:
        """生成 SSE 格式的数据流。

        每个 chunk 经 ``json.dumps`` 序列化为单行 JSON（``ensure_ascii=False``
        但 JSON 会把真实换行转义成字面 ``\n``，因此输出不含裸换行）。
        正常完成时以 ``data: [DONE]`` 结束；错误事件是终止事件，之后不再
        向客户端发送数据，也不得发送成功终止标记。

        收到错误事件后仍会继续排空内层生成器。Core dispatcher 的配额释放、
        请求日志和账本结算位于其异步生成器循环之后；如果在错误事件的
        ``yield`` 位置直接 ``break``/``aclose``，这些收尾逻辑会被跳过。
        """
        emit_done = True
        terminal_error = False
        try:
            async for chunk in self.completion_gen:
                if terminal_error:
                    # Drain the producer so its post-stream settlement executes,
                    # but never expose data after the terminal error event.
                    continue
                yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
                if isinstance(chunk, dict) and isinstance(chunk.get("error"), dict):
                    terminal_error = True
                    emit_done = False
        except (asyncio.CancelledError, GeneratorExit):
            # Client disconnects must propagate cancellation to Starlette and
            # must not be converted into an SSE error event.
            emit_done = False
            raise
        except Exception as exc:
            emit_done = False
            logger.error(
                "SSE stream generation error: %s",
                type(exc).__name__,
            )
            error_chunk = {
                "error": {
                    "code": "internal_error",
                    "message": "The response stream terminated unexpectedly.",
                },
            }
            yield "data: " + json.dumps(error_chunk, ensure_ascii=False) + "\n\n"
        finally:
            close = getattr(self.completion_gen, "aclose", None)
            if callable(close):
                try:
                    await close()
                except (asyncio.CancelledError, GeneratorExit):
                    raise
                except Exception as exc:
                    logger.warning(
                        "Failed to close upstream SSE generator: %s",
                        type(exc).__name__,
                    )

        if emit_done:
            yield "data: [DONE]\n\n"
