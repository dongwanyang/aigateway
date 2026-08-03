"""Terminal SSE precedence regressions."""
from __future__ import annotations

import json

import pytest

from aigateway_core.route.streaming.sse import SSEGenerator


@pytest.mark.asyncio
async def test_explicit_error_survives_later_drain_exception() -> None:
    cleanup_ran = False

    async def producer():
        nonlocal cleanup_ran
        try:
            yield {
                "error": {
                    "code": "upstream_overloaded",
                    "message": "retry later",
                }
            }
            raise RuntimeError("cleanup path failed")
        finally:
            cleanup_ran = True

    chunks = [chunk async for chunk in SSEGenerator(producer()).generate()]

    assert cleanup_ran is True
    assert len(chunks) == 1
    payload = json.loads(chunks[0].removeprefix("data: ").strip())
    assert payload["error"] == {
        "code": "upstream_overloaded",
        "message": "retry later",
    }
    assert "[DONE]" not in chunks[0]


@pytest.mark.asyncio
async def test_exception_without_explicit_error_uses_sanitized_internal_error() -> None:
    async def producer():
        if False:
            yield {}
        raise RuntimeError("secret provider detail")

    chunks = [chunk async for chunk in SSEGenerator(producer()).generate()]

    assert len(chunks) == 1
    payload = json.loads(chunks[0].removeprefix("data: ").strip())
    assert payload["error"]["code"] == "internal_error"
    assert payload["error"]["message"] == (
        "The response stream terminated unexpectedly."
    )
    assert "secret provider detail" not in chunks[0]
    assert "[DONE]" not in chunks[0]
