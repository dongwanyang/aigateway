"""SSE format generator.

Moved verbatim from ``aigateway_api.streaming.SSEGenerator`` (Task 5).
The FastAPI ``StreamingResponse`` adapter that wraps this generator
remains in ``aigateway_api.streaming.create_sse_response``.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncIterator, Dict

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
