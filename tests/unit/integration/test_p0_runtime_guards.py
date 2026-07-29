import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.dispatch.dispatcher import RequestDispatcher
from aigateway_core.dispatch.pipeline_engine import PipelineEngine
from aigateway_core.route.bridge.litellm_bridge import LiteLLMBridge
from aigateway_core.shared.plugin_registry import PluginRegistry


@pytest.mark.asyncio
async def test_app_factory_lifespan_runs_on_startup(monkeypatch):
    from aigateway_api import main

    events = []

    @asynccontextmanager
    async def fake_lifespan(_app):
        events.append("startup")
        yield
        events.append("shutdown")

    monkeypatch.setattr(main, "lifespan", fake_lifespan)
    test_app = main.create_app()

    async with test_app.router.lifespan_context(test_app):
        assert events == ["startup"]
    assert events == ["startup", "shutdown"]


@pytest.mark.asyncio
async def test_factory_app_is_active_during_its_lifespan(monkeypatch):
    from aigateway_api import main
    from aigateway_api.app_state import get_state

    @asynccontextmanager
    async def fake_lifespan(_app):
        yield

    monkeypatch.setattr(main, "lifespan", fake_lifespan)
    factory_app = main.create_app()

    async with factory_app.router.lifespan_context(factory_app):
        assert get_state() is factory_app.state

    with pytest.raises(RuntimeError, match="no FastAPI application lifespan"):
        get_state()


@pytest.mark.asyncio
async def test_nested_app_lifespans_restore_previous_active_app(monkeypatch):
    from aigateway_api import main
    from aigateway_api.app_state import get_state

    @asynccontextmanager
    async def fake_lifespan(_app):
        yield

    monkeypatch.setattr(main, "lifespan", fake_lifespan)
    outer_app = main.create_app()
    inner_app = main.create_app()

    async with outer_app.router.lifespan_context(outer_app):
        assert get_state() is outer_app.state
        async with inner_app.router.lifespan_context(inner_app):
            assert get_state() is inner_app.state
        assert get_state() is outer_app.state


@pytest.mark.asyncio
async def test_failed_factory_lifespan_does_not_leave_stale_active_app(monkeypatch):
    from aigateway_api import main
    from aigateway_api.app_state import get_state

    @asynccontextmanager
    async def failing_lifespan(_app):
        raise RuntimeError("startup failed")
        yield  # pragma: no cover

    monkeypatch.setattr(main, "lifespan", failing_lifespan)
    factory_app = main.create_app()

    with pytest.raises(RuntimeError, match="startup failed"):
        async with factory_app.router.lifespan_context(factory_app):
            pass

    with pytest.raises(RuntimeError, match="no FastAPI application lifespan"):
        get_state()


@pytest.mark.asyncio
async def test_plugin_timeout_isolated_and_next_plugin_runs():
    calls = []

    class SlowPlugin:
        async def execute(self, ctx):
            await asyncio.sleep(0.1)
            return ctx

    class NextPlugin:
        async def execute(self, ctx):
            calls.append("next")
            return ctx

    registry = PluginRegistry(default_timeout_seconds=0.01)
    registry.register("slow", SlowPlugin, priority=1)
    registry.register("next", NextPlugin, priority=2)
    engine = PipelineEngine(registry)

    ctx = await engine.execute_ctx(PipelineContext(request={}, trace_id="trace"))

    assert calls == ["next"]
    assert ctx.should_stop is False


@pytest.mark.asyncio
async def test_plugin_fail_fast_stops_pipeline():
    calls = []

    class BrokenPlugin:
        async def execute(self, _ctx):
            raise RuntimeError("boom")

    class NextPlugin:
        async def execute(self, ctx):
            calls.append("next")
            return ctx

    registry = PluginRegistry()
    registry.register("broken", BrokenPlugin, priority=1, failure_policy="fail_fast")
    registry.register("next", NextPlugin, priority=2)
    engine = PipelineEngine(registry)

    ctx = await engine.execute_ctx(PipelineContext(request={}, trace_id="trace"))

    assert calls == []
    assert ctx.should_stop is True
    assert "boom" in ctx.extra["pipeline_error"]


def test_plugin_registry_applies_per_plugin_policy():
    class Plugin:
        async def execute(self, ctx):
            return ctx

    registry = PluginRegistry(
        default_timeout_seconds=30,
        policies={
            "guarded": {
                "timeout_seconds": 2.5,
                "failure_policy": "fail_fast",
            }
        },
    )
    registry.register("guarded", Plugin)

    plugin = registry.get_all()[0]
    assert plugin.timeout_seconds == 2.5
    assert plugin.failure_policy == "fail_fast"


@pytest.mark.asyncio
async def test_request_deadline_returns_504(monkeypatch):
    config = MagicMock()
    config.get.return_value = 0.01
    dispatcher = RequestDispatcher({"config_manager": config})

    async def slow_dispatch(*_args):
        await asyncio.sleep(0.1)

    monkeypatch.setattr(dispatcher, "_dispatch", AsyncMock(side_effect=slow_dispatch))
    monkeypatch.setattr(dispatcher, "_release_quota_reservation", AsyncMock())

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    request.state.trace_id = "deadline-trace"
    response = await dispatcher.dispatch(SimpleNamespace(), request)

    assert response.status_code == 504
    assert b"request_deadline_exceeded" in response.body


@pytest.mark.asyncio
async def test_retry_budget_caps_gateway_attempts_and_router_retries():
    bridge = LiteLLMBridge(
        config={
            "retry_budget": {
                "max_attempts": 2,
                "max_time_seconds": 1,
                "max_fallback": 1,
            },
            "retry_delay_ms": 0,
        }
    )
    bridge._model_alias_map = {
        "primary": "openai/primary",
        "fallback-a": "openai/fallback-a",
        "fallback-b": "openai/fallback-b",
    }
    bridge._model_capabilities = {
        "primary": ["text"],
        "fallback-a": ["text"],
        "fallback-b": ["text"],
    }
    bridge.router = MagicMock()
    bridge.router.get_model_list.return_value = []
    bridge.router.acompletion = AsyncMock(side_effect=RuntimeError("upstream down"))

    result = await bridge.completion(
        messages=[{"role": "user", "content": "hello"}],
        model="primary",
        fallback_chain=["fallback-a", "fallback-b"],
        max_retries=99,
    )

    assert result["error"]["code"] == "upstream_timeout"
    assert bridge.router.acompletion.await_count == 2
    attempted_models = [
        call.kwargs["model"] for call in bridge.router.acompletion.await_args_list
    ]
    assert attempted_models == ["openai/primary", "openai/fallback-a"]


def test_litellm_router_has_no_nested_retry_or_fallback():
    providers = {
        "openai": {
            "api_key": "test",
            "num_retries": 9,
            "model_grouper": [
                {
                    "models": ["primary"],
                    "fallback_models": ["fallback-a"],
                }
            ],
        }
    }
    bridge = LiteLLMBridge(config={"providers": providers})
    model_list = bridge._build_model_list(providers)

    primary = next(item for item in model_list if item["model_name"].endswith("/primary"))
    assert primary["litellm_params"]["num_retries"] == 0
    assert primary["fallbacks"] == []
    assert bridge._model_fallbacks["primary"] == ["fallback-a"]


def test_grafana_has_no_default_admin_password():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert "GF_SECURITY_ADMIN_PASSWORD=admin" not in compose
    assert 'profiles: ["monitoring"]' in compose
    assert "GRAFANA_ADMIN_PASSWORD must be set" in compose
