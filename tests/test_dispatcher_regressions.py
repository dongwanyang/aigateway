"""Regression tests for dispatcher post-processing paths."""

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

from aigateway_core.dispatch.dispatcher import RequestDispatcher


class _FakeCache:
    def __init__(self) -> None:
        self.l1_items = []
        self.l2_search_store = AsyncMock()

    def l1_set(self, key, value):
        self.l1_items.append((key, value))


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
