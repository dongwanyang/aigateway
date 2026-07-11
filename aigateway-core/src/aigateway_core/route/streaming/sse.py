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
        """生成 SSE 格式的数据流。

        每个 chunk 经 ``json.dumps`` 序列化为单行 JSON（``ensure_ascii=False``
        但 JSON 会把真实换行转义成字面 ``\n``，因此输出不含裸换行），
        直接作为一条 SSE ``data:`` 事件发出，末尾 ``data: [DONE]`` 结束。
        不做额外转义——之前的 ``_escape_sse`` 会把 JSON 里的 ``\n`` 再翻倍成
        ``\\n``，导致客户端 JSON 解析后得到字面 "反斜杠 n" 而非换行，
        破坏代码块等含换行的内容。
        """
        try:
            async for chunk in self.completion_gen:
                yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
        except Exception as exc:
            logger.error("SSE stream generation error: %s", exc)
            error_chunk = {
                "error": {"code": "internal_error", "message": str(exc)},
            }
            yield "data: " + json.dumps(error_chunk, ensure_ascii=False) + "\n\n"
        finally:
            yield "data: [DONE]\n\n"
