"""Regression tests for dispatcher post-processing paths."""

import hashlib
import json
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.responses import JSONResponse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

from aigateway_core.dispatch.classifier import ClassificationResult
from aigateway_core.dispatch.dispatcher import RequestDispatcher


def test_resolve_identity_uses_full_sha256_key_hash():
    request = SimpleNamespace(
        state=SimpleNamespace(
            api_key_data={"user_id": "user-1"},
            api_key_value="gw-test-secret",
        )
    )

    user_id, key_hash = RequestDispatcher._resolve_identity(request)

    assert user_id == "user-1"
    assert key_hash == hashlib.sha256(b"gw-test-secret").hexdigest()
    assert len(key_hash) == 64


@pytest.mark.asyncio
async def test_generation_dispatch_preserves_only_reference_image_urls():
    original_messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "turn this dog into a watercolor"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,cmVmZXJlbmNl"},
                },
            ],
        }
    ]
    optimized_messages = [
        {"role": "user", "content": "turn this dog into a watercolor\n[dog image]"}
    ]
    body = SimpleNamespace(
        messages=original_messages,
        model="auto",
        stream=False,
    )
    request = SimpleNamespace(
        headers={},
        state=SimpleNamespace(
            trace_id="trace-reference-image",
            api_key_data={},
        ),
    )
    dispatcher = RequestDispatcher({"generation_engine": object()})
    expected_response = JSONResponse({"ok": True})

    with (
        patch(
            "aigateway_api.openai_compat._apply_media_optimization",
            new=AsyncMock(
                return_value={"messages": optimized_messages, "meta": {"images": 1}}
            ),
        ),
        patch(
            "aigateway_api.openai_compat._apply_pii_detection",
            new=AsyncMock(
                return_value={"messages": optimized_messages, "meta": {}}
            ),
        ),
        patch(
            "aigateway_core.dispatch.dispatcher.classify_request",
            new=AsyncMock(
                return_value=ClassificationResult("generation:image")
            ),
        ),
        patch.object(
            dispatcher,
            "_dispatch_generation",
            new=AsyncMock(return_value=expected_response),
        ) as dispatch_generation,
    ):
        response = await dispatcher._dispatch(body, request)

    assert response is expected_response
    prefix = dispatch_generation.await_args.args[5]
    assert prefix["reference_image_urls"] == [
        "data:image/png;base64,cmVmZXJlbmNl"
    ]
    assert "reference_messages" not in prefix
    assert body.messages == optimized_messages


class _FakeCache:
    def __init__(self) -> None:
        self.l1_items = []
        self.l2_search_store = AsyncMock()

    def l1_set(self, key, value):
        self.l1_items.append((key, value))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected_status"),
    [
        ("local_backend_unavailable", 503),
        ("invalid_generation_options", 400),
    ],
)
async def test_generation_preflight_error_releases_reserved_quota(
    reason,
    expected_status,
):
    key_store = MagicMock()
    key_store.check_quota = AsyncMock(return_value=(True, "", 0))
    key_store.release_reserved_usage = AsyncMock()
    dispatcher = RequestDispatcher({"key_store": key_store})

    class Engine:
        async def execute_ctx(self, ctx):
            ctx.extra["generation_optimization"] = {
                "draft_generator": {
                    "applicable": False,
                    "reason": reason,
                    "local_error": "preflight failed",
                }
            }
            return ctx

    request = SimpleNamespace(
        headers={},
        state=SimpleNamespace(
            trace_id="trace-preflight",
            api_key_data={"group_id": "grp-test"},
        ),
    )
    body = SimpleNamespace(
        messages=[{"role": "user", "content": "draw a cat"}],
        model="auto",
        stream=False,
        generation_options=SimpleNamespace(
            model_dump=lambda **_kwargs: {"backend": "local"}
        ),
    )

    response = await dispatcher._dispatch_generation(
        body,
        request,
        Engine(),
        user_id="user-1",
        key_hash="key-hash",
        prefix={
            "plugin_trace": [],
            "request_start_time": time.time(),
        },
    )

    assert response.status_code == expected_status
    key_store.release_reserved_usage.assert_awaited_once()
    assert request.state._lua_quota_reserved is False


async def _stream_chunks():
    yield {
        "id": "chatcmpl-test",
        "model": "resolved-model",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hel"}}],
    }
    yield {
        "id": "chatcmpl-test",
        "model": "resolved-model",
        "choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        "_meta": {"cost": 0.42, "routed_to": {"provider": "test-provider", "model": "resolved-model"}},
    }


@pytest.mark.asyncio
async def test_stream_wrapper_backfills_cache_with_scope_metadata():
    dispatcher = RequestDispatcher({})
    cache = _FakeCache()
    key_store = MagicMock()
    key_store.record_request_cost = AsyncMock()
    key_store.increment_usage = AsyncMock()
    request = SimpleNamespace(
        state=SimpleNamespace(trace_id="trace-1", request_id="req-1", _lua_quota_reserved=True),
    )

    with patch("aigateway_api.openai_compat._record_request_log", new=AsyncMock()):
        chunks = [
            chunk
            async for chunk in dispatcher._wrap_stream_full(
                _stream_chunks(),
                metrics_collector=None,
                cache_manager=cache,
                key_store=key_store,
                request=request,
                model="auto",
                user_id="user-1",
                key_hash="key-hash",
                cache_key="cache-key",
                normalized_messages='[{"role":"user","content":"hello"}]',
                llm_start=0,
                group_id="group-1",
                pipeline_kind="understanding",
                cache_scope="group",
                l2_scope_id="group-1",
            )
        ]

    assert len(chunks) == 2
    assert cache.l1_items
    cached = json.loads(cache.l1_items[0][1])
    assert cached["choices"][0]["message"]["content"] == "hello"
    cache.l2_search_store.assert_awaited_once()
    meta = cache.l2_search_store.await_args.kwargs["meta"]
    assert meta["cache_scope"] == "group"
    assert meta["scope_id"] == "group-1"
    assert meta["model_family"] == "auto"
    key_store.increment_usage.assert_awaited_once()
    assert key_store.increment_usage.await_args.kwargs["cost"] == 0.42


@pytest.mark.asyncio
async def test_nonstream_video_id_inside_data_meta_registers_task():
    task_tracker = MagicMock()
    task_tracker.register = AsyncMock()
    bridge = MagicMock()
    bridge.completion = AsyncMock(return_value={
        "data": {
            "choices": [{"message": {"role": "assistant", "content": "Video id=vid_1"}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "_meta": {"video_id": "vid_1"},
        },
        "_meta": {"routed_to": {"model": "agnes-video-v2.0", "intent": "generation:video"}, "cost": 0.0},
        "usage": {},
    })
    dispatcher = RequestDispatcher({"task_tracker": task_tracker})
    request = SimpleNamespace(state=SimpleNamespace(trace_id="trace-2", request_id="req-2"))
    body = SimpleNamespace(
        messages=[{"role": "user", "content": "生成视频"}],
        model="auto",
        temperature=1.0,
        max_tokens=None,
        top_p=1.0,
        frequency_penalty=0.0,
        presence_penalty=0.0,
        tools=None,
        tool_choice=None,
        stop=None,
    )

    with patch("aigateway_api.openai_compat._record_request_log", new=AsyncMock()):
        response = await dispatcher._call_llm_nonstream(
            body,
            request,
            bridge,
            plugin_trace=[],
            request_start_time=0,
            user_id="user-1",
            key_hash=None,
            cache_key=None,
            normalized_messages=None,
            pipeline_kind="generation:video",
            group_id="group-1",
        )

    assert response.status_code == 200
    task_tracker.register.assert_awaited_once()
    assert task_tracker.register.await_args.kwargs["task_id"] == "vid_1"



@pytest.mark.asyncio
async def test_nonstream_unpriced_cost_is_safe_for_numeric_sinks():
    key_store = MagicMock()
    key_store.increment_usage = AsyncMock()
    key_store.record_request_cost = AsyncMock()
    bridge = MagicMock()
    bridge.completion = AsyncMock(return_value={
        "data": {
            "model": "unpriced-model",
            "choices": [
                {"message": {"role": "assistant", "content": "ok"}}
            ],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
            },
        },
        "usage": {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "total_tokens": 5,
        },
        "_meta": {
            "cost": 0.0,
            "routed_to": {
                "provider": "test-provider",
                "model": "unpriced-model",
            },
        },
    })
    dispatcher = RequestDispatcher({"key_store": key_store})
    request = SimpleNamespace(
        state=SimpleNamespace(trace_id="trace-unpriced", request_id="req-unpriced")
    )
    body = SimpleNamespace(
        messages=[{"role": "user", "content": "hello"}],
        model="auto",
        temperature=1.0,
        max_tokens=None,
        top_p=1.0,
        frequency_penalty=0.0,
        presence_penalty=0.0,
        tools=None,
        tool_choice=None,
        stop=None,
    )

    with (
        patch(
            "aigateway_api.openai_compat._record_request_log",
            new=AsyncMock(),
        ),
        patch(
            "aigateway_core.route.metrics.costing._estimate_cost",
            return_value=None,
        ),
    ):
        response = await dispatcher._call_llm_nonstream(
            body,
            request,
            bridge,
            plugin_trace=[],
            request_start_time=time.time(),
            user_id="user-1",
            key_hash="key-hash",
            cache_key=None,
            normalized_messages=None,
            pipeline_kind="understanding",
            group_id="group-1",
        )

    assert response.status_code == 200
    key_store.record_request_cost.assert_awaited_once()
    assert key_store.record_request_cost.await_args.kwargs["cost_usd"] == 0.0
    key_store.increment_usage.assert_awaited_once()
    assert key_store.increment_usage.await_args.kwargs["cost"] == 0.0


async def _unpriced_stream_chunks():
    yield {
        "id": "chatcmpl-unpriced",
        "model": "unpriced-model",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "total_tokens": 5,
        },
        "_meta": {
            "cost": 0.0,
            "routed_to": {
                "provider": "test-provider",
                "model": "unpriced-model",
            },
        },
    }


@pytest.mark.asyncio
async def test_stream_unpriced_cost_is_safe_for_numeric_sinks():
    key_store = MagicMock()
    key_store.increment_usage = AsyncMock()
    key_store.record_request_cost = AsyncMock()
    dispatcher = RequestDispatcher({})
    request = SimpleNamespace(
        state=SimpleNamespace(trace_id="trace-stream-unpriced", request_id="req-stream-unpriced")
    )

    with (
        patch(
            "aigateway_api.openai_compat._record_request_log",
            new=AsyncMock(),
        ),
        patch(
            "aigateway_core.route.metrics.costing._estimate_cost",
            return_value=None,
        ),
    ):
        chunks = [
            chunk
            async for chunk in dispatcher._wrap_stream_full(
                _unpriced_stream_chunks(),
                metrics_collector=None,
                cache_manager=None,
                key_store=key_store,
                request=request,
                model="auto",
                user_id="user-1",
                key_hash="key-hash",
                cache_key=None,
                normalized_messages=None,
                llm_start=time.time(),
                group_id="group-1",
                pipeline_kind="understanding",
            )
        ]

    assert len(chunks) == 1
    key_store.record_request_cost.assert_awaited_once()
    assert key_store.record_request_cost.await_args.kwargs["cost_usd"] == 0.0
    key_store.increment_usage.assert_awaited_once()
    assert key_store.increment_usage.await_args.kwargs["cost"] == 0.0
