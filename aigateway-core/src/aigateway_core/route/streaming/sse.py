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
        """Generate SSE data and emit a terminal outcome after producer cleanup.

        Normal chunks are streamed immediately. An error chunk is buffered while
        the inner producer is drained so quota, request-log and ledger settlement
        can finish before the client receives the terminal event. No data after
        the first error is exposed and a failed stream never emits ``[DONE]``.
        """
        emit_done = True
        terminal_error_event: str | None = None
        try:
            async for chunk in self.completion_gen:
                if terminal_error_event is not None:
                    # Drain the producer without exposing post-terminal data.
                    continue
                event = "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
                if isinstance(chunk, dict) and isinstance(chunk.get("error"), dict):
                    terminal_error_event = event
                    emit_done = False
                    continue
                yield event
        except (asyncio.CancelledError, GeneratorExit):
            emit_done = False
            raise
        except Exception as exc:
            emit_done = False
            logger.error("SSE stream generation error: %s", type(exc).__name__)
            terminal_error_event = "data: " + json.dumps(
                {
                    "error": {
                        "code": "internal_error",
                        "message": "The response stream terminated unexpectedly.",
                    }
                },
                ensure_ascii=False,
            ) + "\n\n"
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

        if terminal_error_event is not None:
            yield terminal_error_event
        elif emit_done:
            yield "data: [DONE]\n\n"
