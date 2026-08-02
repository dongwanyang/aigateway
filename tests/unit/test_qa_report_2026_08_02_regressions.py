"""Regression coverage for the 2026-08-02 control-panel QA report."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aigateway_api import gpu_routes
from aigateway_api.dispatcher import (
    RequestDispatcher,
    _empty_length_limited_data,
    _guard_sse_output,
)
from aigateway_core.pipelines.generation._common.config import DraftWorkflowConfig
from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)
from aigateway_core.route.metrics.costing import estimate_model_cost
from aigateway_core.route.streaming.sse import SSEGenerator
from aigateway_core.shared.gpu_scheduler import (
    GpuResourceCoordinator,
    GpuSchedulerConfig,
)
from aigateway_core.shared.integration_configs import ComfyUIConfig
from aigateway_core.shared.plugin_registry import PluginRegistry
from aigateway_core.shared.runtime_values import configured_model_pricing


@pytest.mark.parametrize(
    "model",
    ["agnes-2.0-flash", "deepseek-v4-flash", "deepseek-v4-pro"],
)
def test_configured_text_prices_are_per_token(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
) -> None:
    config_path = Path(__file__).resolve().parents[2] / "config.yaml"
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", str(config_path))

    pricing = configured_model_pricing(model)

    assert pricing == {"prompt": 0.00000002, "completion": 0.000001}


def test_small_request_cost_is_not_inflated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = Path(__file__).resolve().parents[2] / "config.yaml"
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", str(config_path))

    estimate = estimate_model_cost(
        "agnes-2.0-flash",
        prompt_tokens=286,
        completion_tokens=20,
    )

    assert estimate.status == "priced"
    assert estimate.amount_usd == 0.000026


@pytest.mark.asyncio
async def test_empty_gpu_topology_uses_direct_comfyui_compatibility_path(
    tmp_path: Path,
) -> None:
    strategy = DraftGeneratorStrategy(
        DraftWorkflowConfig(store_dir=str(tmp_path)),
        comfyui_config=ComfyUIConfig(workflow_version="test"),
        store_dir=str(tmp_path),
    )
    coordinator = GpuResourceCoordinator(
        GpuSchedulerConfig.from_mapping({"generation_wait_timeout_seconds": 120}),
        devices=[],
        workers=[],
    )
    strategy._gpu_coordinator = coordinator
    calls = 0

    async def operation() -> bytes:
        nonlocal calls
        calls += 1
        return b"submitted"

    result, worker = await asyncio.wait_for(
        strategy._run_on_comfy_worker("draft-test", "image", operation),
        timeout=0.1,
    )

    assert result == b"submitted"
    assert worker is None
    assert calls == 1
    await coordinator.close()


def test_comfyui_only_topology_is_reported_as_delegated_execution() -> None:
    gateway = gpu_routes._normalize_gateway_topology(
        {
            "available": False,
            "torch_initialized": False,
            "error": "gpu_status_unavailable",
        },
        comfy_available=True,
        scheduler={"devices": [], "workers": []},
    )
    execution = gpu_routes._execution_gpu_status(
        gateway,
        comfy_available=True,
        normalized_comfy_memory={
            "total_bytes": 16_000,
            "free_bytes": 15_000,
            "used_bytes": 1_000,
        },
        scheduler={"devices": [], "workers": []},
    )

    assert gateway["available"] is False
    assert gateway["local_cuda_available"] is False
    assert gateway["status"] == "delegated"
    assert gateway["delegated_to"] == "comfyui"
    assert gateway["error"] is None
    assert execution == {
        "available": True,
        "mode": "delegated_comfyui",
        "owner": "comfyui",
        "memory": {
            "total_bytes": 16_000,
            "free_bytes": 15_000,
            "used_bytes": 1_000,
        },
    }


@pytest.mark.asyncio
async def test_low_text_output_budget_is_rejected_explicitly() -> None:
    dispatcher = RequestDispatcher({})
    body = SimpleNamespace(
        model="agnes-2.0-flash",
        max_tokens=10,
        generation_options=None,
    )

    response = await dispatcher.dispatch(body, SimpleNamespace())
    payload = json.loads(response.body)

    assert response.status_code == 422
    assert payload["error"]["code"] == "output_budget_exhausted"
    assert payload["error"]["param"] == "max_tokens"
    assert payload["error"]["details"]["minimum_recommended"] == 32


def test_empty_length_limited_nonstream_response_is_detected() -> None:
    exhausted, completion_tokens = _empty_length_limited_data(
        {
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": ""},
                    "finish_reason": "length",
                }
            ],
            "usage": {"completion_tokens": 10},
        }
    )

    assert exhausted is True
    assert completion_tokens == 10


@pytest.mark.asyncio
async def test_empty_length_limited_stream_ends_with_error_not_done() -> None:
    async def upstream():
        yield 'data: {"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
        yield 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"length"}],"usage":{"completion_tokens":10}}\n\n'
        yield "data: [DONE]\n\n"

    chunks = [
        chunk
        async for chunk in _guard_sse_output(upstream(), max_tokens=10)
    ]

    assert any("output_budget_exhausted" in chunk for chunk in chunks)
    assert all("[DONE]" not in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_successful_text_stream_keeps_single_done_marker() -> None:
    async def upstream():
        yield 'data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}\n\n'
        yield 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        yield "data: [DONE]\n\n"

    chunks = [
        chunk
        async for chunk in _guard_sse_output(upstream(), max_tokens=10)
    ]

    assert sum("[DONE]" in chunk for chunk in chunks) == 1
    assert all("output_budget_exhausted" not in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_sse_upstream_error_chunk_is_terminal() -> None:
    async def upstream():
        yield {"error": {"code": "upstream_error", "message": "all failed"}}

    chunks = [chunk async for chunk in SSEGenerator(upstream()).generate()]

    assert len(chunks) == 1
    assert '"code": "upstream_error"' in chunks[0]
    assert all("[DONE]" not in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_sse_exception_is_terminal() -> None:
    async def upstream():
        yield {"choices": [{"delta": {"content": "partial"}}]}
        raise RuntimeError("provider disconnected")

    chunks = [chunk async for chunk in SSEGenerator(upstream()).generate()]

    assert len(chunks) == 2
    assert '"content": "partial"' in chunks[0]
    assert '"code": "internal_error"' in chunks[1]
    assert all("[DONE]" not in chunk for chunk in chunks)


def test_plugin_registry_reuses_runtime_instances() -> None:
    construction_count = 0

    class StatefulPlugin:
        async def execute(self, ctx: Any) -> Any:
            return ctx

        def __init__(self) -> None:
            nonlocal construction_count
            construction_count += 1

    registry = PluginRegistry()
    registry.register("stateful", StatefulPlugin)

    first = registry.get_all()[0]
    second = registry.get_all()[0]

    assert first is second
    assert construction_count == 1
