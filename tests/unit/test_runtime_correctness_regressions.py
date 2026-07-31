from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry, Gauge

from aigateway_api import admin_routes, local_generation
from aigateway_api.main import _register_exception_handlers


def test_5xx_responses_never_include_exception_details() -> None:
    app = FastAPI()
    _register_exception_handlers(app)

    @app.get("/unhandled")
    async def unhandled() -> None:
        raise RuntimeError("redis://user:super-secret@example:6379/0")

    @app.get("/http")
    async def http_error() -> None:
        raise HTTPException(
            500,
            detail={
                "error": {
                    "code": "upstream_failed",
                    "message": "postgres://user:secret@db/internal",
                }
            },
        )

    client = TestClient(app, raise_server_exceptions=False)
    for path, code in (("/unhandled", "internal_error"), ("/http", "upstream_failed")):
        response = client.get(path)
        assert response.status_code == 500
        assert response.json() == {
            "error": {"code": code, "message": "Internal Server Error"}
        }
        assert "secret" not in response.text
        assert "detail" not in response.text


@pytest.mark.asyncio
async def test_quota_update_returns_persisted_values(monkeypatch: pytest.MonkeyPatch) -> None:
    class Store:
        def __init__(self) -> None:
            self.data = {
                "key_id": "key_demo",
                "user_id": "user-demo",
                "daily_tokens_limit": "10",
                "monthly_cost_limit": "5",
                "rate_limit_rpm": "3",
                "rate_limit_tpm": "40",
            }

        async def _find_key_hashes_by_id(self, key_id: str) -> list[str]:
            return ["hash"] if key_id == "key_demo" else []

        async def get_api_key(self, key_hash: str) -> dict[str, str]:
            return dict(self.data)

        async def set_api_key(self, key_hash: str, values: dict[str, str]) -> None:
            self.data.update(values)

    store = Store()
    monkeypatch.setattr(
        admin_routes,
        "_get_keystore_and_metrics",
        lambda _request: (store, None),
    )
    response = await admin_routes.update_api_key_quota(
        SimpleNamespace(),
        "key_demo",
        admin_routes.UpdateQuotaRequest(daily_tokens=250, rate_limit_rpm=12),
        {},
    )
    assert response["data"]["quotas"]["daily_tokens_limit"] == 250
    assert response["data"]["quotas"]["rate_limit_rpm"] == 12


@pytest.mark.asyncio
async def test_plugin_toggle_rejects_coerced_boolean() -> None:
    class Request:
        async def json(self) -> dict[str, object]:
            return {"name": "prompt_cache", "enabled": "false"}

    with pytest.raises(HTTPException) as exc_info:
        await admin_routes.update_plugins_config(Request(), {})
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_metrics_json_preserves_all_label_series(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = CollectorRegistry()
    gauge = Gauge("runtime_probe", "probe", ["worker"], registry=registry)
    gauge.labels(worker="a").set(1)
    gauge.labels(worker="b").set(2)

    class Connection:
        def fetchall(self, _query: str) -> list[object]:
            return []

    state = SimpleNamespace(
        metrics_collector=SimpleNamespace(
            _registry=registry,
            get_uptime_seconds=lambda: 1,
        ),
        key_store=SimpleNamespace(conn=Connection()),
        litellm_bridge=None,
    )
    monkeypatch.setattr("aigateway_api.app_state.get_state", lambda: state)
    response = await admin_routes.get_metrics_json(SimpleNamespace(), {})
    series = response["data"]["prometheus_series"]["runtime_probe"]
    assert {item["labels"]["worker"] for item in series} == {"a", "b"}
    assert response["data"]["prometheus"]["runtime_probe"] in series


@pytest.mark.asyncio
async def test_comfyui_probe_keeps_partial_status(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self.payload

    class Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str) -> Response:
            if url.endswith("/queue"):
                raise httpx.ConnectError("queue unavailable")
            if url.endswith("/system_stats"):
                return Response({"devices": [{"name": "GPU", "vram_total": 10}]})
            return Response({"KSampler": {}})

    monkeypatch.setattr(local_generation.httpx, "AsyncClient", Client)
    result = await local_generation.probe_comfyui(
        {
            "server_url": "http://comfyui:8188",
            "public_url": "http://localhost:8188",
            "models_path": "/missing",
            "connect_timeout": 1,
            "read_timeout": 1,
            "checkpoint_name": "model.safetensors",
        }
    )
    assert result["available"] is True
    assert result["gpu"]["name"] == "GPU"
    assert result["queue"] is None
    assert result["endpoint_errors"]["queue"] == "ConnectError"
