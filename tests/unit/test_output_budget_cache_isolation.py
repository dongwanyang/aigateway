"""Concurrency regression coverage for tiny output-budget cache bypass."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.responses import JSONResponse

from aigateway_api.dispatcher import RequestDispatcher, _ORIGINAL_DISPATCH_ATTR


@pytest.mark.asyncio
async def test_tiny_budget_uses_request_local_dispatcher_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_manager = object()
    dispatcher = RequestDispatcher({"cache_manager": cache_manager})
    observed_targets: list[Any] = []

    async def original_dispatch(self: Any, body: Any, request: Any) -> JSONResponse:
        observed_targets.append(self)
        assert self.cache_manager is None
        return JSONResponse(
            content={
                "data": {
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ]
                },
                "message": "success",
            }
        )

    monkeypatch.setattr(RequestDispatcher, _ORIGINAL_DISPATCH_ATTR, original_dispatch)
    body = SimpleNamespace(
        model="agnes-2.0-flash",
        max_tokens=10,
        generation_options=None,
    )

    response = await dispatcher.dispatch(body, SimpleNamespace())

    assert response.status_code == 200
    assert observed_targets and observed_targets[0] is not dispatcher
    assert dispatcher.cache_manager is cache_manager
