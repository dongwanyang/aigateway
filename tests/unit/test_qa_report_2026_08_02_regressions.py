"""Regression coverage for the 2026-08-02 control-panel QA report."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

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
