"""RequestDispatcher adapter and API-boundary response guards.

The orchestration implementation lives in ``aigateway_core.dispatch.dispatcher``.
This module keeps the backward-compatible API import surface and adds protocol
normalization that belongs at the HTTP boundary rather than in provider routing.
"""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi.responses import JSONResponse, StreamingResponse

from aigateway_core.dispatch.classifier import classify_request
from aigateway_core.dispatch.dispatcher import RequestDispatcher as CoreRequestDispatcher

_MIN_TEXT_OUTPUT_TOKENS = 32
_ORIGINAL_DISPATCH_ATTR = "_aigateway_api_original_dispatch"
_GUARDED_DISPATCH_ATTR = "_aigateway_api_output_guard"


def _is_text_completion(body: Any) -> bool:
    """Return whether the request expects an assistant text response."""
    model = str(getattr(body, "model", "") or "").lower()
    if getattr(body, "generation_options", None) is not None:
        return False
    return not any(marker in model for marker in ("image", "video"))


def _output_budget_error(max_tokens: int | None, completion_tokens: int = 0) -> dict[str, Any]:
    return {
        "error": {
            "code": "output_budget_exhausted",
            "message": (
                "The output token budget was exhausted before the upstream model "
                "produced assistant content. Increase max_tokens and retry."
            ),
            "type": "invalid_request_error",
            "param": "max_tokens",
            "details": {
                "max_tokens": max_tokens,
                "minimum_recommended": _MIN_TEXT_OUTPUT_TOKENS,
                "completion_tokens": max(0, int(completion_tokens or 0)),
                "finish_reason": "length",
            },
        }
    }


def _empty_length_limited_data(data: Any) -> tuple[bool, int]:
    """Detect a completion that consumed its budget without usable output."""
    if not isinstance(data, dict):
        return False, 0
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return False, 0

    saw_length = False
    saw_usable_output = False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        if choice.get("finish_reason") == "length":
            saw_length = True
        message = choice.get("message") or {}
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                saw_usable_output = True
            if message.get("tool_calls") or message.get("function_call"):
                saw_usable_output = True

    usage = data.get("usage") or {}
    completion_tokens = (
        int(usage.get("completion_tokens", 0) or 0)
        if isinstance(usage, dict)
        else 0
    )
    return saw_length and not saw_usable_output, completion_tokens


def _parse_sse_payload(raw: str | bytes) -> dict[str, Any] | None:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    stripped = text.strip()
    if not stripped.startswith("data:") or stripped == "data: [DONE]":
        return None
    payload = stripped[5:].strip()
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


async def _guard_sse_output(
    iterator: AsyncIterator[str | bytes],
    *,
    max_tokens: int | None,
) -> AsyncIterator[str | bytes]:
    """Replace an empty length-limited success terminator with an SSE error."""
    saw_content = False
    saw_length = False
    saw_error = False
    completion_tokens = 0
    done_chunk: str | bytes | None = None

    async for raw in iterator:
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        if text.strip() == "data: [DONE]":
            done_chunk = raw
            continue

        payload = _parse_sse_payload(raw)
        if payload is not None:
            if isinstance(payload.get("error"), dict):
                saw_error = True
            usage = payload.get("usage") or {}
            if isinstance(usage, dict):
                completion_tokens = max(
                    completion_tokens,
                    int(usage.get("completion_tokens", 0) or 0),
                )
            for choice in payload.get("choices", []) or []:
                if not isinstance(choice, dict):
                    continue
                if choice.get("finish_reason") == "length":
                    saw_length = True
                delta = choice.get("delta") or choice.get("message") or {}
                if isinstance(delta, dict):
                    content = delta.get("content")
                    if isinstance(content, str) and content.strip():
                        saw_content = True
                    if delta.get("tool_calls") or delta.get("function_call"):
                        saw_content = True
        yield raw

    if saw_error:
        return
    if saw_length and not saw_content:
        error_event = "data: " + json.dumps(
            _output_budget_error(max_tokens, completion_tokens),
            ensure_ascii=False,
        ) + "\n\n"
        yield error_event.encode("utf-8") if isinstance(done_chunk, bytes) else error_event
        return
    if done_chunk is not None:
        yield done_chunk


async def _dispatch_with_output_guard(self: Any, body: Any, request: Any):
    max_tokens = getattr(body, "max_tokens", None)
    text_completion = _is_text_completion(body)
    bypass_cache = (
        text_completion
        and isinstance(max_tokens, int)
        and 0 < max_tokens < _MIN_TEXT_OUTPUT_TOKENS
    )
    dispatch_target = self
    if bypass_cache:
        # Cache keys bucket max_tokens. Tiny budgets must not share a bucket
        # with larger requests because an empty length-limited response could
        # otherwise poison subsequent completions, or a tiny request could
        # receive content generated with a larger budget. Use a request-local
        # shallow copy so concurrent requests never observe a mutated shared
        # dispatcher instance.
        dispatch_target = copy.copy(self)
        dispatch_target.cache_manager = None

    original_dispatch = getattr(type(self), _ORIGINAL_DISPATCH_ATTR)
    response = await original_dispatch(dispatch_target, body, request)

    if not text_completion:
        return response

    if isinstance(response, StreamingResponse):
        response.body_iterator = _guard_sse_output(
            response.body_iterator,
            max_tokens=max_tokens,
        )
        return response

    if isinstance(response, JSONResponse) and response.status_code == 200:
        try:
            payload = json.loads(response.body)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            return response
        exhausted, completion_tokens = _empty_length_limited_data(
            payload.get("data") if isinstance(payload, dict) else None
        )
        if exhausted:
            error_payload = _output_budget_error(max_tokens, completion_tokens)
            if isinstance(payload, dict) and payload.get("_meta"):
                error_payload["_meta"] = payload["_meta"]
            return JSONResponse(content=error_payload, status_code=422)

    return response


if not hasattr(CoreRequestDispatcher, _ORIGINAL_DISPATCH_ATTR):
    setattr(
        CoreRequestDispatcher,
        _ORIGINAL_DISPATCH_ATTR,
        CoreRequestDispatcher.dispatch,
    )
if not getattr(CoreRequestDispatcher.dispatch, _GUARDED_DISPATCH_ATTR, False):
    setattr(_dispatch_with_output_guard, _GUARDED_DISPATCH_ATTR, True)
    CoreRequestDispatcher.dispatch = _dispatch_with_output_guard

# Preserve the runtime-structure contract: API and Core imports are the same
# class object, while the API module installs an idempotent HTTP-boundary guard.
RequestDispatcher = CoreRequestDispatcher


__all__ = ["RequestDispatcher", "classify_request"]
