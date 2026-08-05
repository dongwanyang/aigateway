"""RequestDispatcher adapter and API-boundary response guards.

The orchestration implementation lives in ``aigateway_core.dispatch.dispatcher``.
This module keeps the backward-compatible API import surface and installs
request-scoped protocol guards without mutating shared dispatcher state.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any, Self

from aigateway_core.dispatch.classifier import classify_request
from aigateway_core.dispatch.dispatcher import (
    RequestDispatcher as CoreRequestDispatcher,
)
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

_MIN_TEXT_OUTPUT_TOKENS = 32
_ORIGINAL_DISPATCH_ATTR = "_aigateway_api_original_dispatch"
_ORIGINAL_NONSTREAM_ATTR = "_aigateway_api_original_call_llm_nonstream"
_ORIGINAL_STREAM_ATTR = "_aigateway_api_original_call_llm_stream"
_GUARDED_DISPATCH_ATTR = "_aigateway_api_output_guard"
_GUARDED_BRIDGE_ATTR = "_aigateway_api_bridge_output_guard"
_LOG_ORIGINAL_ATTR = "_aigateway_api_original_record_request_log"
_LOG_GUARD_ATTR = "_aigateway_api_output_status_guard"


def _is_text_completion(body: Any) -> bool:
    """Return whether the request may produce an assistant text response.

    Generation options are routing hints, not a reliable modality signal. Auto
    requests can carry image/video controls and still be classified as text from
    the prompt, so only an explicitly media-named model bypasses text guards.
    """
    model = str(getattr(body, "model", "") or "").lower()
    return not any(marker in model for marker in ("image", "video"))


def _output_budget_error(
    max_tokens: int | None,
    completion_tokens: int = 0,
) -> dict[str, Any]:
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


def _upstream_stream_error(usage: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized_usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    if isinstance(usage, dict):
        for key in normalized_usage:
            normalized_usage[key] = max(0, int(usage.get(key, 0) or 0))
    return {
        "error": {
            "code": "upstream_stream_error",
            "message": "The upstream model stream terminated before completion.",
            "type": "upstream_error",
        },
        # A non-empty zero usage object lets Core persist a failure ledger row.
        # Core separately releases the reservation when total_tokens remains 0.
        "usage": normalized_usage,
    }


def _choice_has_output(choice: dict[str, Any]) -> bool:
    message = choice.get("message") or choice.get("delta") or {}
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    return bool(
        isinstance(content, str)
        and content.strip()
        or message.get("tool_calls")
        or message.get("function_call")
    )


def _empty_length_limited_data(data: Any) -> tuple[bool, int]:
    """Detect an all-length completion that contains no usable assistant output."""
    if not isinstance(data, dict):
        return False, 0
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return False, 0

    valid_choices = [choice for choice in choices if isinstance(choice, dict)]
    if not valid_choices:
        return False, 0
    all_length_limited = all(
        choice.get("finish_reason") == "length" for choice in valid_choices
    )
    saw_usable_output = any(_choice_has_output(choice) for choice in valid_choices)

    usage = data.get("usage") or {}
    completion_tokens = (
        int(usage.get("completion_tokens", 0) or 0)
        if isinstance(usage, dict)
        else 0
    )
    return all_length_limited and not saw_usable_output, completion_tokens


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


def _request_state(request: Any) -> Any:
    state = getattr(request, "state", None)
    if state is None:
        state = type("RequestState", (), {})()
        request.state = state
    return state


def _mark_output_budget_exhausted(
    request: Any,
    completion_tokens: int = 0,
) -> None:
    state = _request_state(request)
    state._output_budget_exhausted = True
    state._output_budget_completion_tokens = max(
        int(getattr(state, "_output_budget_completion_tokens", 0) or 0),
        max(0, int(completion_tokens or 0)),
    )


def _is_output_budget_exhausted(request: Any) -> bool:
    return bool(
        getattr(_request_state(request), "_output_budget_exhausted", False)
    )


def _mark_upstream_stream_failed(request: Any) -> None:
    _request_state(request)._upstream_stream_failed = True


def _is_upstream_stream_failed(request: Any) -> bool:
    return bool(getattr(_request_state(request), "_upstream_stream_failed", False))


def _mark_client_disconnected(request: Any) -> None:
    _request_state(request)._client_disconnected = True


def _is_client_disconnected(request: Any) -> bool:
    return bool(getattr(_request_state(request), "_client_disconnected", False))


def _terminal_status_code(request: Any) -> int | None:
    if _is_output_budget_exhausted(request):
        return 422
    if _is_upstream_stream_failed(request):
        return 502
    if _is_client_disconnected(request):
        return 499
    return None


def _terminal_ledger_status(request: Any) -> str | None:
    if _is_output_budget_exhausted(request):
        return "output_budget_exhausted"
    if _is_upstream_stream_failed(request):
        return "upstream_stream_error"
    if _is_client_disconnected(request):
        return "client_disconnected"
    return None


def _cached_data(value: Any) -> dict[str, Any] | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(value, dict):
        return None
    nested = value.get("data")
    return nested if isinstance(nested, dict) else value


class _RequestCacheProxy:
    """Disable unsafe cache reads/writes for one request without shared mutation."""

    def __init__(self, target: Any, request: Any, *, bypass_all: bool) -> None:
        self._target = target
        self._request = request
        self._bypass_all = bypass_all

    @property
    def _qdrant_client(self) -> Any:
        if self._blocked():
            return None
        return getattr(self._target, "_qdrant_client", None)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)

    def _blocked(self) -> bool:
        return self._bypass_all or _terminal_status_code(self._request) is not None

    def generate_cache_key(self, *args: Any, **kwargs: Any) -> Any:
        return self._target.generate_cache_key(*args, **kwargs)

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        if self._bypass_all:
            return None
        cached = await self._target.get(*args, **kwargs)
        if isinstance(cached, dict):
            exhausted, _ = _empty_length_limited_data(
                _cached_data(cached.get("value"))
            )
            if exhausted:
                return None
        return cached

    def l1_set(self, *args: Any, **kwargs: Any) -> Any:
        if self._blocked():
            return None
        return self._target.l1_set(*args, **kwargs)

    async def l2_search_store(self, *args: Any, **kwargs: Any) -> Any:
        if self._blocked():
            return None
        return await self._target.l2_search_store(*args, **kwargs)

    async def l3_store(self, *args: Any, **kwargs: Any) -> Any:
        if self._blocked():
            return None
        return await self._target.l3_store(*args, **kwargs)


class _RequestKeyStoreProxy:
    """Preserve token accounting while marking terminal outputs as failures."""

    def __init__(self, target: Any, request: Any) -> None:
        self._target = target
        self._request = request

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)

    async def record_request_cost(self, *args: Any, **kwargs: Any) -> Any:
        terminal_status = _terminal_ledger_status(self._request)
        if terminal_status is not None and kwargs.get("status") == "ok":
            kwargs = dict(kwargs)
            kwargs["status"] = terminal_status
        return await self._target.record_request_cost(*args, **kwargs)


class _NoopRequestTracker:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


class _RequestMetricsProxy:
    """Defer request status/duration until the final HTTP/SSE outcome is known."""

    def __init__(self, target: Any) -> None:
        self._target = target

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)

    def track_request(self, *_args: Any, **_kwargs: Any) -> _NoopRequestTracker:
        return _NoopRequestTracker()

    def record_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def record_duration(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _OutputGuardBridge:
    """Observe provider results before Core performs logging, ledger, or caching."""

    def __init__(self, target: Any, request: Any) -> None:
        self._target = target
        self._request = request

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)

    async def completion(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._target.completion(*args, **kwargs)
        if isinstance(result, dict):
            exhausted, completion_tokens = _empty_length_limited_data(
                result.get("data")
            )
            if exhausted:
                _mark_output_budget_exhausted(self._request, completion_tokens)
        return result

    def completion_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        upstream = self._target.completion_stream(*args, **kwargs)
        return _inspect_upstream_stream(upstream, self._request)


async def _inspect_upstream_stream(
    iterator: AsyncIterator[Any],
    request: Any,
) -> AsyncIterator[Any]:
    """Convert provider failures into one terminal chunk before Core cleanup.

    The Core stream wrapper performs quota settlement, request logging and ledger
    writes only after its input iterator finishes. Converting exceptions here
    lets that wrapper finish normally; the SSE formatter can suppress ``[DONE]``
    without closing the Core generator at a suspended ``yield`` point.
    """
    saw_content = False
    terminal_reasons: list[str] = []
    completion_tokens = 0
    last_usage: dict[str, Any] = {}
    completed_normally = False

    try:
        async for original_chunk in iterator:
            chunk = original_chunk
            if isinstance(chunk, dict):
                usage = chunk.get("usage") or {}
                if isinstance(usage, dict) and usage:
                    last_usage = dict(usage)
                    completion_tokens = max(
                        completion_tokens,
                        int(usage.get("completion_tokens", 0) or 0),
                    )
                if isinstance(chunk.get("error"), dict):
                    _mark_upstream_stream_failed(request)
                    if not isinstance(chunk.get("usage"), dict) or not chunk.get("usage"):
                        chunk = dict(chunk)
                        chunk["usage"] = _upstream_stream_error(last_usage)["usage"]
                    yield chunk
                    return
                for choice in chunk.get("choices", []) or []:
                    if not isinstance(choice, dict):
                        continue
                    if _choice_has_output(choice):
                        saw_content = True
                    finish_reason = choice.get("finish_reason")
                    if isinstance(finish_reason, str) and finish_reason:
                        terminal_reasons.append(finish_reason)
            yield chunk
        completed_normally = True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _mark_upstream_stream_failed(request)
        logger.warning(
            "Upstream model stream failed before completion: %s",
            type(exc).__name__,
        )
        yield _upstream_stream_error(last_usage)
    finally:
        close = getattr(iterator, "aclose", None)
        if callable(close):
            try:
                await close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(
                    "Upstream stream close failed: %s",
                    type(exc).__name__,
                )

    if (
        completed_normally
        and terminal_reasons
        and all(reason == "length" for reason in terminal_reasons)
        and not saw_content
    ):
        _mark_output_budget_exhausted(request, completion_tokens)


def _install_request_log_guard() -> None:
    """Make Core request logging honor the request-scoped final status marker."""
    try:
        from aigateway_api import openai_compat
    except (ImportError, AttributeError):
        return

    current = getattr(openai_compat, "_record_request_log", None)
    if current is None:
        return
    if not hasattr(openai_compat, _LOG_ORIGINAL_ATTR):
        setattr(openai_compat, _LOG_ORIGINAL_ATTR, current)
    if getattr(current, _LOG_GUARD_ATTR, False):
        return

    original = getattr(openai_compat, _LOG_ORIGINAL_ATTR)

    async def guarded_record_request_log(*args: Any, **kwargs: Any) -> Any:
        request = kwargs.get("request")
        terminal_status = (
            _terminal_status_code(request) if request is not None else None
        )
        if terminal_status is not None and kwargs.get("status_code") == 200:
            kwargs = dict(kwargs)
            kwargs["status_code"] = terminal_status
        return await original(*args, **kwargs)

    setattr(guarded_record_request_log, _LOG_GUARD_ATTR, True)
    openai_compat._record_request_log = guarded_record_request_log


def _record_final_metrics(
    metrics_collector: Any,
    *,
    status_code: int,
    started_at: float,
) -> None:
    if metrics_collector is None:
        return
    try:
        metrics_collector.record_request(
            "POST",
            "/v1/chat/completions",
            str(status_code),
        )
        metrics_collector.record_duration(
            "/v1/chat/completions",
            max(0.0, time.monotonic() - started_at),
        )
    except Exception:
        # Metrics must not become a request correctness dependency.
        return


async def _guard_sse_output(
    iterator: AsyncIterator[str | bytes],
    *,
    max_tokens: int | None,
    request: Any | None = None,
    metrics_collector: Any = None,
    started_at: float | None = None,
) -> AsyncIterator[str | bytes]:
    """Emit one final SSE outcome and settle consumer cancellation as 499."""
    saw_content = False
    terminal_reasons: list[str] = []
    saw_error = False
    completion_tokens = 0
    done_chunk: str | bytes | None = None
    error_chunk: str | bytes | None = None
    emitted_bytes = False

    try:
        async for raw in iterator:
            emitted_bytes = emitted_bytes or isinstance(raw, bytes)
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            if text.strip() == "data: [DONE]":
                done_chunk = raw
                continue
            if saw_error:
                continue

            payload = _parse_sse_payload(raw)
            if payload is not None:
                if isinstance(payload.get("error"), dict):
                    saw_error = True
                    error_chunk = raw
                    continue
                usage = payload.get("usage") or {}
                if isinstance(usage, dict):
                    completion_tokens = max(
                        completion_tokens,
                        int(usage.get("completion_tokens", 0) or 0),
                    )
                for choice in payload.get("choices", []) or []:
                    if not isinstance(choice, dict):
                        continue
                    if _choice_has_output(choice):
                        saw_content = True
                    finish_reason = choice.get("finish_reason")
                    if isinstance(finish_reason, str) and finish_reason:
                        terminal_reasons.append(finish_reason)
            yield raw
    except (asyncio.CancelledError, GeneratorExit):
        if request is not None:
            _mark_client_disconnected(request)
        status_code = (
            _terminal_status_code(request)
            if request is not None
            else 499
        ) or 499
        if started_at is not None:
            _record_final_metrics(
                metrics_collector,
                status_code=status_code,
                started_at=started_at,
            )
        close = getattr(iterator, "aclose", None)
        if callable(close):
            try:
                await close()
            except (asyncio.CancelledError, GeneratorExit):
                pass
            except Exception as exc:
                logger.debug(
                    "Cancelled stream close failed: %s",
                    type(exc).__name__,
                )
        raise

    exhausted = bool(
        _is_output_budget_exhausted(request)
        if request is not None
        else False
    ) or bool(
        terminal_reasons
        and all(reason == "length" for reason in terminal_reasons)
        and not saw_content
    )
    if exhausted and request is not None:
        _mark_output_budget_exhausted(request, completion_tokens)

    terminal_status = _terminal_status_code(request) if request is not None else None
    status_code = terminal_status or (502 if saw_error else 200)
    if started_at is not None:
        _record_final_metrics(
            metrics_collector,
            status_code=status_code,
            started_at=started_at,
        )

    if saw_error:
        if error_chunk is not None:
            yield error_chunk
        return
    if exhausted:
        error_event = "data: " + json.dumps(
            _output_budget_error(max_tokens, completion_tokens),
            ensure_ascii=False,
        ) + "\n\n"
        use_bytes = isinstance(done_chunk, bytes) or emitted_bytes
        yield error_event.encode("utf-8") if use_bytes else error_event
        return
    if done_chunk is not None:
        yield done_chunk


async def _call_llm_nonstream_with_guard(
    self: Any,
    body: Any,
    request: Any,
    litellm_bridge: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    original = getattr(type(self), _ORIGINAL_NONSTREAM_ATTR)
    bridge = (
        litellm_bridge
        if isinstance(litellm_bridge, _OutputGuardBridge)
        else _OutputGuardBridge(litellm_bridge, request)
    )
    return await original(self, body, request, bridge, *args, **kwargs)


async def _call_llm_stream_with_guard(
    self: Any,
    body: Any,
    request: Any,
    litellm_bridge: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    original = getattr(type(self), _ORIGINAL_STREAM_ATTR)
    bridge = (
        litellm_bridge
        if isinstance(litellm_bridge, _OutputGuardBridge)
        else _OutputGuardBridge(litellm_bridge, request)
    )
    return await original(self, body, request, bridge, *args, **kwargs)


async def _dispatch_with_output_guard(self: Any, body: Any, request: Any) -> Any:
    _install_request_log_guard()
    if not _is_text_completion(body):
        original_dispatch = getattr(type(self), _ORIGINAL_DISPATCH_ATTR)
        return await original_dispatch(self, body, request)

    started_at = time.monotonic()
    max_tokens = getattr(body, "max_tokens", None)
    bypass_cache = (
        isinstance(max_tokens, int)
        and 0 < max_tokens < _MIN_TEXT_OUTPUT_TOKENS
    )
    dispatch_target = copy.copy(self)
    metrics_collector = getattr(self, "metrics_collector", None)

    cache_manager = getattr(self, "cache_manager", None)
    if cache_manager is not None:
        # Treat malformed or partially initialized cache objects as unavailable.
        # Production CacheManager instances expose this minimal interface; test
        # doubles and degraded startup states may not.
        if all(
            hasattr(cache_manager, name)
            for name in ("generate_cache_key", "get", "l1_set", "l2_search_store")
        ):
            dispatch_target.cache_manager = _RequestCacheProxy(
                cache_manager,
                request,
                bypass_all=bypass_cache,
            )
        else:
            dispatch_target.cache_manager = None
    key_store = getattr(self, "key_store", None)
    if key_store is not None:
        dispatch_target.key_store = _RequestKeyStoreProxy(key_store, request)
    if metrics_collector is not None:
        dispatch_target.metrics_collector = _RequestMetricsProxy(metrics_collector)

    state = getattr(self, "state", None)
    if isinstance(state, dict):
        dispatch_target.state = dict(state)
        dispatch_target.state["cache_manager"] = getattr(
            dispatch_target, "cache_manager", None
        )
        dispatch_target.state["key_store"] = getattr(
            dispatch_target, "key_store", None
        )
        dispatch_target.state["metrics_collector"] = getattr(
            dispatch_target, "metrics_collector", None
        )

    original_dispatch = getattr(type(self), _ORIGINAL_DISPATCH_ATTR)
    try:
        response = await original_dispatch(dispatch_target, body, request)
    except Exception:
        _record_final_metrics(
            metrics_collector,
            status_code=500,
            started_at=started_at,
        )
        raise

    if isinstance(response, StreamingResponse):
        response.body_iterator = _guard_sse_output(
            response.body_iterator,
            max_tokens=max_tokens,
            request=request,
            metrics_collector=metrics_collector,
            started_at=started_at,
        )
        return response

    if isinstance(response, JSONResponse) and response.status_code == 200:
        try:
            payload = json.loads(response.body)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            payload = None
        exhausted, completion_tokens = _empty_length_limited_data(
            payload.get("data") if isinstance(payload, dict) else None
        )
        if exhausted:
            _mark_output_budget_exhausted(request, completion_tokens)
            error_payload = _output_budget_error(max_tokens, completion_tokens)
            if isinstance(payload, dict) and payload.get("_meta"):
                error_payload["_meta"] = payload["_meta"]
            response = JSONResponse(content=error_payload, status_code=422)

    _record_final_metrics(
        metrics_collector,
        status_code=int(getattr(response, "status_code", 500)),
        started_at=started_at,
    )
    return response


if not hasattr(CoreRequestDispatcher, _ORIGINAL_NONSTREAM_ATTR):
    setattr(
        CoreRequestDispatcher,
        _ORIGINAL_NONSTREAM_ATTR,
        CoreRequestDispatcher._call_llm_nonstream,
    )
if not getattr(
    CoreRequestDispatcher._call_llm_nonstream,
    _GUARDED_BRIDGE_ATTR,
    False,
):
    setattr(_call_llm_nonstream_with_guard, _GUARDED_BRIDGE_ATTR, True)
    CoreRequestDispatcher._call_llm_nonstream = _call_llm_nonstream_with_guard

if not hasattr(CoreRequestDispatcher, _ORIGINAL_STREAM_ATTR):
    setattr(
        CoreRequestDispatcher,
        _ORIGINAL_STREAM_ATTR,
        CoreRequestDispatcher._call_llm_stream,
    )
if not getattr(CoreRequestDispatcher._call_llm_stream, _GUARDED_BRIDGE_ATTR, False):
    setattr(_call_llm_stream_with_guard, _GUARDED_BRIDGE_ATTR, True)
    CoreRequestDispatcher._call_llm_stream = _call_llm_stream_with_guard

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
# class object, while this API module installs idempotent request-bound guards.
RequestDispatcher = CoreRequestDispatcher


__all__ = ["RequestDispatcher", "classify_request"]
