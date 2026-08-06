"""Generation observability metrics must be visible at the public endpoint."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from prometheus_client import CollectorRegistry

from aigateway_api import app_state, routes
from aigateway_core.pipelines.generation._common.metrics import (
    get_prometheus_registry,
    reset_prometheus_registry,
)


@pytest.fixture(autouse=True)
def _fresh_generation_registry():
    reset_prometheus_registry()
    yield
    reset_prometheus_registry()


@pytest.mark.asyncio
async def test_metrics_endpoint_includes_generation_registry(monkeypatch):
    generation = get_prometheus_registry()
    generation.inc_video_reference_source("source_draft")
    generation.inc_video_keyframe("success")

    state = SimpleNamespace(
        metrics_collector=SimpleNamespace(_registry=CollectorRegistry()),
        litellm_bridge=None,
    )
    monkeypatch.setattr(app_state, "get_state", lambda request=None: state)

    response = await routes.get_metrics(SimpleNamespace())
    body = response.body.decode()

    assert response.status_code == 200
    assert (
        'gen_opt_video_reference_source_total{source_kind="source_draft"} 1.0'
        in body
    )
    assert 'gen_opt_video_keyframe_total{outcome="success"} 1.0' in body
