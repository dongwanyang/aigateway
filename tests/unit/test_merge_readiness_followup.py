"""Production-path regressions found during PR #26 self-review."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aigateway_api import openai_compat
from aigateway_api.dispatcher import (
    RequestDispatcher,
    _LOG_GUARD_ATTR,
    _LOG_ORIGINAL_ATTR,
    _RequestCacheProxy,
    _RequestKeyStoreProxy,
    _guard_sse_output,
    _inspect_upstream_stream,
    _install_request_log_guard,
    _mark_output_budget_exhausted,
)
from aigateway_api.gpu_routes import (
    _execution_gpu_status,
    _normalize_gateway_topology,
    _scheduler_runnable_pairs,
)
from aigateway_core.pipelines.generation._common.config import (
    DraftWorkflowConfig,
    ModelRouterConfig,
)
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)
from aigateway_core.prefix.cache.l3_semantic import _safe_l3_backfill
from aigateway_core.route.model_resolution.model_router import ModelRouterStrategy
from aigateway_core.route.streaming.sse import SSEGenerator
from aigateway_core.shared.gpu_scheduler import (
    GpuDevice,
    GpuResourceCoordinator,
    GpuSchedulerConfig,
    workers_from_config,
)
from aigateway_core.shared.integration_configs import ComfyUIConfig
from aigateway_core.shared.plugin_registry import PluginRegistry


class _RecordingCache:
    def __init__(self) -> None:
        self._qdrant_client = object()
        self.writes: list[str] = []

    def generate_cache_key(self, *_args: Any, **_kwargs: Any) -> str:
        return "key"

    async def get(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def l1_set(self, *_args: Any, **_kwargs: Any) -> None:
        self.writes.append("l1")

    async def l2_search_store(self, *_args: Any, **_kwargs: Any) -> None:
        self.writes.append("l2")

    async def l3_store(self, *_args: Any, **_kwargs: Any) -> None:
        self.writes.append("l3")


class _RecordingKeyStore:
    def __init__(self) -> None:
        self.ledger_statuses: list[str] = []
        self.increment_calls: list[dict[str, Any]] = []
        self.release_calls: list[dict[str, Any]] = []

    async def record_request_cost(self, **kwargs: Any) -> None:
        self.ledger_statuses.append(str(kwargs.get("status")))

    async def increment_usage(self, *_args: Any, **kwargs: Any) -> None:
        self.increment_calls.append(dict(kwargs))

    async def release_reserved_usage(self, *_args: Any, **kwargs: Any) -> None:
        self.release_calls.append(dict(kwargs))


@pytest.mark.asyncio
async def test_exhausted_response_cannot_reach_l3_semantic_cache() -> None:
    request = SimpleNamespace(state=SimpleNamespace())
    target = _RecordingCache()
    proxy = _RequestCacheProxy(target, request, bypass_all=False)
    _mark_output_budget_exhausted(request, 64)

    assert proxy._qdrant_client is None
    await _safe_l3_backfill(
        proxy,
        "key",
        json.dumps({"choices": []}),
        "messages",
        "model",
        "user",
        64,
    )
    await proxy.l3_store(prompt_hash="key")

    assert target.writes == []


@pytest.mark.asyncio
async def test_terminal_sse_error_drains_producer_cleanup() -> None:
    cleanup_ran = False

    async def producer():
        nonlocal cleanup_ran
        try:
            yield {"error": {"code": "upstream_error", "message": "failed"}}
            yield {"choices": [{"delta": {"content": "must-not-leak"}}]}
        finally:
            cleanup_ran = True

    chunks = [chunk async for chunk in SSEGenerator(producer()).generate()]

    assert cleanup_ran is True
    assert len(chunks) == 1
    assert "upstream_error" in chunks[0]
    assert "must-not-leak" not in chunks[0]
    assert all("[DONE]" not in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_provider_exception_runs_core_stream_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(
            trace_id="trace",
            request_id="request",
            _lua_quota_reserved=True,
            _lua_reserved_tokens=10,
            _lua_reserved_cost=0.0,
        )
    )
    cache = _RecordingCache()
    cache_proxy = _RequestCacheProxy(cache, request, bypass_all=False)
    key_store = _RecordingKeyStore()
    key_proxy = _RequestKeyStoreProxy(key_store, request)
    logged_statuses: list[int] = []

    async def record_log(**kwargs: Any) -> None:
        logged_statuses.append(int(kwargs["status_code"]))

    # Install the production request-log guard around this test recorder.
    monkeypatch.delattr(openai_compat, _LOG_ORIGINAL_ATTR, raising=False)
    monkeypatch.setattr(openai_compat, "_record_request_log", record_log)
    _install_request_log_guard()
    assert getattr(openai_compat._record_request_log, _LOG_GUARD_ATTR, False)

    async def provider():
        yield {
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "partial"},
                    "finish_reason": None,
                }
            ],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 1,
                "total_tokens": 4,
            },
        }
        raise RuntimeError("provider disconnected")

    dispatcher = RequestDispatcher({})
    inspected = _inspect_upstream_stream(provider(), request)
    settled = dispatcher._wrap_stream_full(
        inspected,
        None,
        cache_proxy,
        key_proxy,
        request,
        "test-model",
        "user",
        "key-hash",
        "cache-key",
        "messages",
        time.time(),
        "group",
        "understanding",
        "group",
        "group",
    )
    chunks = [chunk async for chunk in SSEGenerator(settled).generate()]

    assert any("partial" in chunk for chunk in chunks)
    assert any("upstream_stream_error" in chunk for chunk in chunks)
    assert all("[DONE]" not in chunk for chunk in chunks)
    assert logged_statuses == [502]
    assert key_store.ledger_statuses == ["upstream_stream_error"]
    assert len(key_store.increment_calls) == 1
    assert cache.writes == []
    assert request.state._upstream_stream_failed is True


@pytest.mark.asyncio
async def test_output_guard_suppresses_done_after_existing_error_event() -> None:
    async def upstream():
        yield 'data: {"error":{"code":"upstream_error","message":"failed"}}\n\n'
        yield "data: [DONE]\n\n"

    chunks = [
        chunk
        async for chunk in _guard_sse_output(upstream(), max_tokens=64)
    ]
    assert len(chunks) == 1
    assert "upstream_error" in chunks[0]
    assert "[DONE]" not in chunks[0]


def test_enabled_scheduler_never_synthesizes_worker_from_fixed_url() -> None:
    devices = [GpuDevice("GPU-local", 0, total_memory_gb=16, free_memory_gb=15)]
    config = {
        "gpu_scheduler": {"enabled": True, "workers": []},
        "generation_optimization": {
            "draft_workflow": {
                "comfyui": {"server_url": "https://remote-comfy.example"}
            }
        },
    }

    assert workers_from_config(config, devices) == []


def test_disabled_scheduler_keeps_legacy_single_url_compatibility() -> None:
    devices = [GpuDevice("GPU-local", 0, total_memory_gb=16, free_memory_gb=15)]
    config = {
        "gpu_scheduler": {"enabled": False, "workers": []},
        "generation_optimization": {
            "draft_workflow": {
                "comfyui": {"server_url": "http://localhost:8188"}
            }
        },
    }

    workers = workers_from_config(config, devices)
    assert len(workers) == 1
    assert workers[0].device_uuid == "GPU-local"


@pytest.mark.asyncio
async def test_external_comfyui_does_not_acquire_local_generation_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AIGATEWAY_SHARED_GPU", raising=False)
    strategy = DraftGeneratorStrategy(
        DraftWorkflowConfig(store_dir=str(tmp_path)),
        comfyui_config=ComfyUIConfig(
            server_url="https://remote-comfy.example",
            workflow_version="test",
            scheduler_managed=False,
        ),
        store_dir=str(tmp_path),
    )
    coordinator = GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping({"enabled": True}),
        devices=[GpuDevice("GPU-local", 0, total_memory_gb=16, free_memory_gb=15)],
        workers=[],
    )
    strategy._gpu_coordinator = coordinator
    called = 0

    async def operation() -> str:
        nonlocal called
        called += 1
        assert strategy._server_url() == "https://remote-comfy.example"
        return "submitted"

    result, worker = await strategy._run_on_comfy_worker(
        "draft",
        "image",
        operation,
    )

    assert result == "submitted"
    assert worker is None
    assert called == 1
    await coordinator.close()


@pytest.mark.asyncio
async def test_managed_pool_fails_closed_without_explicit_topology(
    tmp_path: Path,
) -> None:
    strategy = DraftGeneratorStrategy(
        DraftWorkflowConfig(store_dir=str(tmp_path)),
        comfyui_config=ComfyUIConfig(
            server_url="http://comfyui:8188",
            workflow_version="test",
            scheduler_managed=True,
        ),
        store_dir=str(tmp_path),
    )
    coordinator = GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping({"enabled": True}),
        devices=[GpuDevice("GPU-local", 0, total_memory_gb=16, free_memory_gb=15)],
        workers=[],
    )
    strategy._gpu_coordinator = coordinator

    with pytest.raises(DraftWorkflowError, match="gpu_scheduler_topology_unavailable"):
        await strategy._run_on_comfy_worker(
            "draft",
            "image",
            lambda: None,
        )
    await coordinator.close()


@pytest.mark.parametrize(
    "worker_patch,scheduler_patch",
    [
        ({"healthy": False}, {}),
        ({"oom_quarantine_remaining_seconds": 30}, {}),
        ({"unhealthy_cooldown_remaining_seconds": 30}, {}),
        ({}, {"comfyui_devices": ["GPU-other"]}),
        ({"capabilities": ["image"]}, {}),
    ],
)
def test_structurally_valid_pool_reports_runtime_degraded(
    worker_patch: dict[str, Any],
    scheduler_patch: dict[str, Any],
) -> None:
    worker = {
        "worker_id": "worker",
        "device_uuid": "GPU-a",
        "capabilities": ["image", "video"],
        "healthy": True,
        "unhealthy_cooldown_remaining_seconds": 0,
        "oom_quarantine_remaining_seconds": 0,
        **worker_patch,
    }
    scheduler = {
        "enabled": True,
        "devices": [{"uuid": "GPU-a"}],
        "workers": [worker],
        **scheduler_patch,
    }
    capability = "video" if worker_patch.get("capabilities") == ["image"] else None
    assert _scheduler_runnable_pairs(scheduler, capability=capability) == []

    gateway = _normalize_gateway_topology(
        {"available": True, "error": None},
        comfy_available=True,
        scheduler=scheduler,
        pool_expected=True,
    )
    execution = _execution_gpu_status(
        gateway,
        comfy_available=True,
        normalized_comfy_memory=None,
        scheduler=scheduler,
        pool_expected=True,
    )

    # General status remains runnable when only a request-specific capability is
    # absent; the capability-specific assertion above covers that distinction.
    if capability is None:
        assert gateway["status"] == "scheduler_pool_degraded"
        assert gateway["scheduler_topology_complete"] is True
        assert gateway["scheduler_runnable"] is False
        assert execution["mode"] == "scheduler_pool"
        assert execution["topology_complete"] is True
        assert execution["runnable_now"] is False
        assert execution["available"] is False


@pytest.mark.asyncio
async def test_model_router_converts_token_rates_to_request_cost() -> None:
    config = ModelRouterConfig(
        default_model="model-a",
        default_capability_score=50,
        model_capabilities={"model-a": 80, "model-b": 80},
        model_modalities={"model-a": ["text"], "model-b": ["text"]},
    )
    providers = {
        "provider": {
            "model_grouper": [
                {
                    "models": [
                        {"name": "model-a", "capabilities": ["text"]},
                        {"name": "model-b", "capabilities": ["text"]},
                    ],
                    "pricing": {
                        "model-a": {"prompt": 0.0000001, "completion": 0.0001},
                        "model-b": {"prompt": 0.0000002, "completion": 0.000001},
                    },
                }
            ]
        }
    }
    router = ModelRouterStrategy(config, providers)
    by_name = {model.name: model for model in router.get_model_list()}

    assert by_name["model-a"].price_per_request == pytest.approx(0.0501)
    assert by_name["model-b"].price_per_request == pytest.approx(0.0007)
    decision = await router.route(
        complexity_score=50,
        required_modality="text",
        routing_hint="cheapest",
    )
    assert decision.selected_model == "model-b"
    assert decision.estimated_cost == pytest.approx(0.0007)


def test_plugin_registry_constructs_heavy_plugin_once_under_concurrency() -> None:
    constructions = 0
    constructions_lock = threading.Lock()

    class HeavyPlugin:
        def __init__(self) -> None:
            nonlocal constructions
            time.sleep(0.02)
            with constructions_lock:
                constructions += 1

        async def execute(self, ctx: Any) -> Any:
            return ctx

    registry = PluginRegistry()
    registry.register("heavy", HeavyPlugin)

    with ThreadPoolExecutor(max_workers=8) as executor:
        instances = list(executor.map(lambda _index: registry.get_all()[0], range(16)))

    assert constructions == 1
    assert all(instance is instances[0] for instance in instances)


@pytest.mark.asyncio
async def test_provider_failure_without_usage_records_ledger_and_releases_quota() -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(
            trace_id="trace-zero",
            request_id="request-zero",
            _lua_quota_reserved=True,
            _lua_reserved_tokens=10,
            _lua_reserved_cost=0.0,
        )
    )
    key_store = _RecordingKeyStore()
    key_proxy = _RequestKeyStoreProxy(key_store, request)

    async def provider():
        if False:
            yield {}
        raise RuntimeError("provider failed before first token")

    dispatcher = RequestDispatcher({})
    settled = dispatcher._wrap_stream_full(
        _inspect_upstream_stream(provider(), request),
        None,
        None,
        key_proxy,
        request,
        "test-model",
        "user",
        "key-hash",
        None,
        None,
        time.time(),
        "group",
        "understanding",
        "group",
        "group",
    )
    chunks = [chunk async for chunk in SSEGenerator(settled).generate()]

    assert any("upstream_stream_error" in chunk for chunk in chunks)
    assert all("[DONE]" not in chunk for chunk in chunks)
    assert key_store.ledger_statuses == ["upstream_stream_error"]
    assert key_store.increment_calls == []
    assert len(key_store.release_calls) == 1
    assert request.state._lua_quota_reserved is False
