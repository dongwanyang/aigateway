"""Regression coverage for the PR #26 merge-blocking review findings."""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from aigateway_api.dispatcher import (
    _OutputGuardBridge,
    _RequestCacheProxy,
    _RequestKeyStoreProxy,
    _guard_sse_output,
)
from aigateway_api.gpu_routes import (
    _execution_gpu_status,
    _normalize_gateway_topology,
)


class RecordingCache:
    def __init__(self, cached: Any = None) -> None:
        self.cached = cached
        self.writes: list[tuple[str, Any]] = []
        self._qdrant_client = object()

    def generate_cache_key(self, *_args: Any, **_kwargs: Any) -> str:
        return "cache-key"

    async def get(self, *_args: Any, **_kwargs: Any) -> Any:
        return self.cached

    def l1_set(self, key: str, value: str) -> None:
        self.writes.append((key, value))

    async def l2_search_store(self, key: str, value: str, **_kwargs: Any) -> None:
        self.writes.append((f"l2:{key}", value))


class RecordingKeyStore:
    def __init__(self) -> None:
        self.statuses: list[str] = []

    async def record_request_cost(self, **kwargs: Any) -> None:
        self.statuses.append(str(kwargs["status"]))


class RecordingMetrics:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str]] = []
        self.durations: list[tuple[str, float]] = []

    def record_request(self, method: str, path: str, status: str) -> None:
        self.requests.append((method, path, status))

    def record_duration(self, path: str, duration: float) -> None:
        self.durations.append((path, duration))


@pytest.mark.asyncio
async def test_tiny_budget_cache_proxy_bypasses_reads_and_writes() -> None:
    request = SimpleNamespace(state=SimpleNamespace())
    target = RecordingCache(cached={"value": "should-not-be-read"})
    proxy = _RequestCacheProxy(target, request, bypass_all=True)

    assert proxy.generate_cache_key() == "cache-key"
    assert await proxy.get("cache-key") is None
    assert proxy._qdrant_client is None

    proxy.l1_set("cache-key", "value")
    await proxy.l2_search_store("cache-key", "value")

    assert target.writes == []


@pytest.mark.asyncio
async def test_invalid_empty_length_cache_entry_is_rejected() -> None:
    cached_value = json.dumps(
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": ""},
                    "finish_reason": "length",
                }
            ],
            "usage": {"completion_tokens": 10},
        }
    )
    request = SimpleNamespace(state=SimpleNamespace())
    target = RecordingCache(cached={"value": cached_value, "hit_tier": "L1"})
    proxy = _RequestCacheProxy(target, request, bypass_all=False)

    assert await proxy.get("cache-key") is None


@pytest.mark.asyncio
async def test_nonstream_exhaustion_blocks_cache_and_marks_ledger_failure() -> None:
    request = SimpleNamespace(state=SimpleNamespace())

    class Bridge:
        async def completion(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "data": {
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": ""},
                            "finish_reason": "length",
                        }
                    ],
                    "usage": {"completion_tokens": 10},
                }
            }

    result = await _OutputGuardBridge(Bridge(), request).completion()
    assert result["data"]["usage"]["completion_tokens"] == 10
    assert request.state._output_budget_exhausted is True

    cache = RecordingCache()
    cache_proxy = _RequestCacheProxy(cache, request, bypass_all=False)
    cache_proxy.l1_set("cache-key", "poison")
    await cache_proxy.l2_search_store("cache-key", "poison")
    assert cache.writes == []

    key_store = RecordingKeyStore()
    await _RequestKeyStoreProxy(key_store, request).record_request_cost(status="ok")
    assert key_store.statuses == ["output_budget_exhausted"]


@pytest.mark.asyncio
async def test_stream_exhaustion_sets_marker_before_post_processing() -> None:
    request = SimpleNamespace(state=SimpleNamespace())

    class Bridge:
        async def _stream(self):
            yield {
                "choices": [
                    {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
                ]
            }
            yield {
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "length"}
                ],
                "usage": {"completion_tokens": 10},
            }

        def completion_stream(self, *_args: Any, **_kwargs: Any):
            return self._stream()

    chunks = [
        chunk
        async for chunk in _OutputGuardBridge(Bridge(), request).completion_stream()
    ]

    assert len(chunks) == 2
    assert request.state._output_budget_exhausted is True
    assert request.state._output_budget_completion_tokens == 10


@pytest.mark.asyncio
async def test_stream_exhaustion_records_422_and_suppresses_done() -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(
            _output_budget_exhausted=True,
            _output_budget_completion_tokens=10,
        )
    )
    metrics = RecordingMetrics()

    async def upstream():
        yield 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"length"}],"usage":{"completion_tokens":10}}\n\n'
        yield "data: [DONE]\n\n"

    chunks = [
        chunk
        async for chunk in _guard_sse_output(
            upstream(),
            max_tokens=10,
            request=request,
            metrics_collector=metrics,
            started_at=0.0,
        )
    ]

    assert any("output_budget_exhausted" in chunk for chunk in chunks)
    assert all("[DONE]" not in chunk for chunk in chunks)
    assert metrics.requests == [("POST", "/v1/chat/completions", "422")]
    assert len(metrics.durations) == 1


@pytest.mark.parametrize(
    "scheduler",
    [
        {"devices": [{"uuid": "gpu-1"}], "workers": []},
        {"devices": [], "workers": [{"worker_id": "w-1", "device_uuid": "gpu-1"}]},
        {
            "devices": [{"uuid": "gpu-1"}],
            "workers": [{"worker_id": "w-1", "device_uuid": "gpu-2"}],
        },
    ],
)
def test_partial_scheduler_topology_is_not_reported_available(
    scheduler: dict[str, Any],
) -> None:
    gateway = _normalize_gateway_topology(
        {"available": False, "error": "gpu_status_unavailable"},
        comfy_available=False,
        scheduler=scheduler,
    )
    execution = _execution_gpu_status(
        gateway,
        comfy_available=False,
        normalized_comfy_memory=None,
        scheduler=scheduler,
    )

    assert gateway["status"] == "unavailable"
    assert gateway["scheduler_topology_complete"] is False
    assert gateway["error"] == "gpu_scheduler_topology_incomplete"
    assert execution["available"] is False
    assert execution["mode"] == "unavailable"


def test_scheduler_pool_requires_matching_worker_device_pair() -> None:
    scheduler = {
        "devices": [{"uuid": "gpu-1"}, {"uuid": "gpu-2"}],
        "workers": [
            {"worker_id": "w-1", "device_uuid": "gpu-1"},
            {"worker_id": "w-orphan", "device_uuid": "gpu-3"},
        ],
    }
    gateway = _normalize_gateway_topology(
        {"available": False, "error": "gpu_status_unavailable"},
        comfy_available=False,
        scheduler=scheduler,
    )
    execution = _execution_gpu_status(
        gateway,
        comfy_available=False,
        normalized_comfy_memory=None,
        scheduler=scheduler,
    )

    assert gateway["status"] == "scheduler_pool"
    assert gateway["scheduler_topology_complete"] is True
    assert execution["available"] is True
    assert execution["device_count"] == 1
    assert execution["worker_count"] == 1
