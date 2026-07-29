"""Unit tests for routes.py — GET /health and GET /metrics endpoints.

Covers all code paths in ``aigateway_api.routes``:
- Healthy, degraded, and unhealthy health checks (Redis + Qdrant)
- Plugin status reporting
- Circuit-breaker status reporting
- Metrics success path (with registry) and error fallback path
- Edge cases: missing attributes, None managers, disabled plugins

Uses a minimal FastAPI app with the router mounted directly, avoiding the
full create_app() lifespan which touches Redis/Qdrant/SQLite.
"""

import asyncio
import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import FastAPI

# Ensure the API package is importable
sys.path.insert(0, "aigateway-api/src")

from aigateway_api.routes import router


class _ASGISyncClient:
    def __init__(self, app: FastAPI) -> None:
        self.app = app

    def get(self, path: str, **kwargs):
        async def send():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://testserver",
            ) as client:
                return await client.get(path, **kwargs)

        return asyncio.run(send())


def _make_app(**state_attrs):
    """Build a minimal FastAPI app with ``router`` mounted and ``app.state`` set."""
    app = FastAPI()
    state = app.state
    for key, val in state_attrs.items():
        setattr(state, key, val)
    app.include_router(router)
    return app


def _client(**state_attrs):
    app = _make_app(**state_attrs)
    return _ASGISyncClient(app), app


# ------------------------------------------------------------------
# GET /health — healthy (all deps connected)
# ------------------------------------------------------------------


class TestHealthHealthy:
    """All critical dependencies healthy."""

    def test_all_deps_connected(self):
        mock_redis_mgr = MagicMock()
        mock_redis_mgr.redis = AsyncMock()
        mock_redis_mgr.redis.ping = AsyncMock(return_value=True)
        mock_qdrant_mgr = MagicMock()
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=MagicMock())
        mock_http.get.return_value.raise_for_status = MagicMock()
        mock_qdrant_mgr._http = mock_http

        client, _ = _client(
            redis_manager=mock_redis_mgr,
            qdrant_manager=mock_qdrant_mgr,
            config_manager=MagicMock(),
            plugin_registry=None,
            _start_time=1000000,
        )
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "healthy"
        assert data["dependencies"]["redis"]["status"] == "connected"
        assert data["dependencies"]["qdrant"]["status"] == "connected"
        assert resp.json()["message"] == "success"

    def test_plugins_reported_when_enabled(self):
        mock_plugin = MagicMock()
        mock_plugin.name = "pii_detector"
        mock_plugin.enabled = True
        mock_registry = MagicMock()
        mock_registry.get_all = MagicMock(return_value=[mock_plugin])

        client, _ = _client(
            redis_manager=None,
            qdrant_manager=None,
            config_manager=None,
            plugin_registry=mock_registry,
            _start_time=0,
        )
        resp = client.get("/health")
        assert resp.status_code == 200
        plugins = resp.json()["data"]["plugins"]
        assert "pii_detector" in plugins
        assert plugins["pii_detector"]["enabled"] is True
        assert plugins["pii_detector"]["status"] == "healthy"

    def test_plugins_reported_when_disabled(self):
        mock_plugin = MagicMock()
        mock_plugin.name = "cache_manager"
        mock_plugin.enabled = False
        mock_registry = MagicMock()
        mock_registry.get_all = MagicMock(return_value=[mock_plugin])

        client, _ = _client(
            redis_manager=None,
            qdrant_manager=None,
            config_manager=None,
            plugin_registry=mock_registry,
            _start_time=0,
        )
        resp = client.get("/health")
        data = resp.json()["data"]["plugins"]
        assert data["cache_manager"]["enabled"] is False
        assert data["cache_manager"]["status"] == "disabled"

    def test_no_plugins_returns_empty_dict(self):
        client, _ = _client(
            redis_manager=None,
            qdrant_manager=None,
            config_manager=None,
            plugin_registry=None,
            _start_time=0,
        )
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["data"]["plugins"] == {}

    def test_uptime_seconds_computed(self):
        client, _ = _client(
            redis_manager=None,
            qdrant_manager=None,
            config_manager=None,
            plugin_registry=None,
            _start_time=1000000,
        )
        resp = client.get("/health")
        uptime = resp.json()["data"]["uptime_seconds"]
        expected = int(datetime.now(UTC).timestamp()) - 1000000
        assert abs(uptime - expected) <= 2

    def test_start_time_zero_returns_zero_uptime(self):
        client, _ = _client(
            redis_manager=None,
            qdrant_manager=None,
            config_manager=None,
            plugin_registry=None,
            _start_time=0,
        )
        resp = client.get("/health")
        assert resp.json()["data"]["uptime_seconds"] == 0

    def test_version_field_present(self):
        client, _ = _client(
            redis_manager=None,
            qdrant_manager=None,
            config_manager=None,
            plugin_registry=None,
            _start_time=0,
        )
        resp = client.get("/health")
        assert resp.json()["data"]["version"] == "1.0.0"

    def test_timestamp_format(self):
        client, _ = _client(
            redis_manager=None,
            qdrant_manager=None,
            config_manager=None,
            plugin_registry=None,
            _start_time=0,
        )
        resp = client.get("/health")
        ts = resp.json()["data"]["timestamp"]
        datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


# ------------------------------------------------------------------
# GET /health — degraded (some error, some connected)
# ------------------------------------------------------------------


class TestHealthDegraded:
    """One dep error, one connected -> degraded."""

    def test_redis_error_qdrant_ok(self):
        mock_redis_mgr = MagicMock()
        mock_redis_mgr.redis = AsyncMock()
        mock_redis_mgr.redis.ping = AsyncMock(side_effect=Exception("conn refused"))
        mock_qdrant_mgr = MagicMock()
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=MagicMock())
        mock_http.get.return_value.raise_for_status = MagicMock()
        mock_qdrant_mgr._http = mock_http

        client, _ = _client(
            redis_manager=mock_redis_mgr,
            qdrant_manager=mock_qdrant_mgr,
            config_manager=MagicMock(),
            plugin_registry=None,
            _start_time=0,
        )
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "degraded"
        assert data["dependencies"]["redis"]["status"] == "error"
        assert data["dependencies"]["qdrant"]["status"] == "connected"
        assert resp.json()["message"] == "partial degradation"

    def test_redis_ok_qdrant_error(self):
        mock_redis_mgr = MagicMock()
        mock_redis_mgr.redis = AsyncMock()
        mock_redis_mgr.redis.ping = AsyncMock(return_value=True)
        mock_qdrant_mgr = MagicMock()
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(side_effect=Exception("not found"))
        mock_qdrant_mgr._http = mock_http

        client, _ = _client(
            redis_manager=mock_redis_mgr,
            qdrant_manager=mock_qdrant_mgr,
            config_manager=MagicMock(),
            plugin_registry=None,
            _start_time=0,
        )
        resp = client.get("/health")
        data = resp.json()["data"]
        assert data["status"] == "degraded"
        assert data["dependencies"]["qdrant"]["status"] == "error"

    def test_both_connected_is_healthy_not_degraded(self):
        mock_redis_mgr = MagicMock()
        mock_redis_mgr.redis = AsyncMock()
        mock_redis_mgr.redis.ping = AsyncMock(return_value=True)
        mock_qdrant_mgr = MagicMock()
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=MagicMock())
        mock_http.get.return_value.raise_for_status = MagicMock()
        mock_qdrant_mgr._http = mock_http

        client, _ = _client(
            redis_manager=mock_redis_mgr,
            qdrant_manager=mock_qdrant_mgr,
            config_manager=MagicMock(),
            plugin_registry=None,
            _start_time=0,
        )
        resp = client.get("/health")
        assert resp.json()["data"]["status"] == "healthy"


# ------------------------------------------------------------------
# GET /health — unhealthy (no deps connected)
# ------------------------------------------------------------------


class TestHealthUnhealthy:
    """Both critical deps down -> unhealthy."""

    def test_both_disconnected(self):
        client, _ = _client(
            redis_manager=MagicMock(redis=None),
            qdrant_manager=MagicMock(_http=None),
            config_manager=MagicMock(),
            plugin_registry=None,
            _start_time=0,
        )
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "unhealthy"
        assert data["dependencies"]["redis"]["status"] == "disconnected"
        assert data["dependencies"]["qdrant"]["status"] == "disconnected"
        assert resp.json()["message"] == "partial degradation"

    def test_both_error(self):
        mock_redis_mgr = MagicMock()
        mock_redis_mgr.redis = AsyncMock()
        mock_redis_mgr.redis.ping = AsyncMock(side_effect=Exception("fail"))
        mock_qdrant_mgr = MagicMock()
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(side_effect=Exception("fail"))
        mock_qdrant_mgr._http = mock_http

        client, _ = _client(
            redis_manager=mock_redis_mgr,
            qdrant_manager=mock_qdrant_mgr,
            config_manager=MagicMock(),
            plugin_registry=None,
            _start_time=0,
        )
        resp = client.get("/health")
        data = resp.json()["data"]
        assert data["status"] == "unhealthy"
        assert data["dependencies"]["redis"]["status"] == "error"
        assert data["dependencies"]["qdrant"]["status"] == "error"

    def test_one_disconnected_one_error_unhealthy(self):
        """One disconnected, one error -> still unhealthy (both not connected)."""
        mock_qdrant_mgr = MagicMock()
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(side_effect=Exception("fail"))
        mock_qdrant_mgr._http = mock_http

        client, _ = _client(
            redis_manager=MagicMock(redis=None),
            qdrant_manager=mock_qdrant_mgr,
            config_manager=MagicMock(),
            plugin_registry=None,
            _start_time=0,
        )
        resp = client.get("/health")
        data = resp.json()["data"]
        assert data["status"] == "unhealthy"


# ------------------------------------------------------------------
# GET /health — circuit breaker status
# ------------------------------------------------------------------


class TestHealthCircuitBreaker:
    """CB status included when litellm_bridge available."""

    def test_cb_status_included(self):
        mock_bridge = MagicMock()
        mock_bridge.get_cooldown_status = MagicMock(return_value={
            "provider-a": {"state": "OPEN", "fails": 5},
        })

        client, _ = _client(
            redis_manager=None,
            qdrant_manager=None,
            config_manager=None,
            plugin_registry=None,
            _start_time=0,
            litellm_bridge=mock_bridge,
        )
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_cb_status_empty_without_bridge(self):
        client, _ = _client(
            redis_manager=None,
            qdrant_manager=None,
            config_manager=None,
            plugin_registry=None,
            _start_time=0,
            litellm_bridge=None,
        )
        resp = client.get("/health")
        assert resp.status_code == 200


# ------------------------------------------------------------------
# GET /health — edge cases
# ------------------------------------------------------------------


class TestHealthEdgeCases:
    """Missing attributes, plugin with no name, etc."""

    def test_missing_optional_attributes(self):
        """Missing redis_manager/qdrant_manager -> getattr returns None -> disconnected."""
        client, _ = _client(
            redis_manager=None,
            qdrant_manager=None,
            config_manager=None,
            plugin_registry=None,
            _start_time=0,
        )
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["dependencies"]["redis"]["status"] == "disconnected"
        assert data["dependencies"]["qdrant"]["status"] == "disconnected"

    def test_plugin_with_no_name_attribute(self):
        """Plugin without 'name' falls back to 'unknown'."""
        mock_plugin = MagicMock(spec=[])
        del mock_plugin.name
        mock_registry = MagicMock()
        mock_registry.get_all = MagicMock(return_value=[mock_plugin])

        client, _ = _client(
            redis_manager=None,
            qdrant_manager=None,
            config_manager=None,
            plugin_registry=mock_registry,
            _start_time=0,
        )
        resp = client.get("/health")
        plugins = resp.json()["data"]["plugins"]
        assert "unknown" in plugins

    def test_plugin_with_no_enabled_attribute(self):
        """Plugin without 'enabled' falls back to True."""
        mock_plugin = MagicMock(spec=[])
        del mock_plugin.enabled
        mock_registry = MagicMock()
        mock_registry.get_all = MagicMock(return_value=[mock_plugin])

        client, _ = _client(
            redis_manager=None,
            qdrant_manager=None,
            config_manager=None,
            plugin_registry=mock_registry,
            _start_time=0,
        )
        resp = client.get("/health")
        plugins = resp.json()["data"]["plugins"]
        assert plugins["unknown"]["enabled"] is True


# ------------------------------------------------------------------
# GET /metrics — success path
# ------------------------------------------------------------------


class TestMetricsSuccess:
    """Normal Prometheus export with metrics_collector present."""

    def test_metrics_returns_text_plain(self):
        mock_registry = MagicMock()
        mock_collector = MagicMock()
        mock_collector._registry = mock_registry

        client, _ = _client(
            metrics_collector=mock_collector,
            litellm_bridge=None,
        )
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]

    def test_metrics_calls_generate_latest_with_registry(self):
        mock_registry = MagicMock()
        mock_collector = MagicMock()
        mock_collector._registry = mock_registry

        client, _ = _client(
            metrics_collector=mock_collector,
            litellm_bridge=None,
        )

        with patch(
            "aigateway_api.routes.generate_latest",
            return_value=b"fake metrics\n",
        ) as mock_gen, patch(
            "aigateway_api.routes.CONTENT_TYPE_LATEST",
            "text/plain; version=0.0.4; charset=utf-8",
        ):
            resp = client.get("/metrics")
            assert resp.status_code == 200
            mock_gen.assert_called_once_with(mock_registry)

    def test_metrics_fallback_registry_when_collector_none(self):
        client, _ = _client(
            metrics_collector=None,
            litellm_bridge=None,
        )
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]

    def test_metrics_fallback_registry_when_collector_no_registry(self):
        mock_collector = MagicMock()
        mock_collector._registry = None

        client, _ = _client(
            metrics_collector=mock_collector,
            litellm_bridge=None,
        )
        resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_metrics_updates_circuit_breaker_state(self):
        mock_registry = MagicMock()
        mock_collector = MagicMock()
        mock_collector._registry = mock_registry
        mock_bridge = MagicMock()
        mock_bridge.get_cooldown_status_by_provider = MagicMock(
            return_value={"provider-a": "OPEN"}
        )

        client, _ = _client(
            metrics_collector=mock_collector,
            litellm_bridge=mock_bridge,
        )
        resp = client.get("/metrics")
        assert resp.status_code == 200
        mock_bridge.get_cooldown_status_by_provider.assert_called_once()
        mock_collector.set_circuit_breaker_state.assert_called_with(
            provider="provider-a", state="OPEN"
        )

    def test_metrics_skips_cb_update_without_method(self):
        mock_registry = MagicMock()
        mock_collector = MagicMock()
        mock_collector._registry = mock_registry
        mock_bridge = MagicMock()
        del mock_bridge.get_cooldown_status_by_provider

        client, _ = _client(
            metrics_collector=mock_collector,
            litellm_bridge=mock_bridge,
        )
        resp = client.get("/metrics")
        assert resp.status_code == 200


# ------------------------------------------------------------------
# GET /metrics — error fallback path
# ------------------------------------------------------------------


class TestMetricsErrorFallback:
    """When generate_latest or collect_all raises, return JSON error."""

    def test_metrics_json_error_on_exception(self):
        mock_registry = MagicMock()
        mock_collector = MagicMock()
        mock_collector._registry = mock_registry

        client, _ = _client(
            metrics_collector=mock_collector,
            litellm_bridge=None,
        )

        with patch(
            "aigateway_api.routes.generate_latest",
            side_effect=RuntimeError("prometheus broken"),
        ):
            resp = client.get("/metrics")
            assert resp.status_code == 500
            assert resp.headers["content-type"] == "application/json"
            body = resp.json()
            assert body["error"]["code"] == "internal_error"
            assert "Failed to collect metrics" in body["error"]["message"]

    def test_metrics_error_without_collector(self):
        client, _ = _client(
            metrics_collector=None,
            litellm_bridge=None,
        )

        with patch(
            "aigateway_api.routes.generate_latest",
            side_effect=ValueError("nothing registered"),
        ):
            resp = client.get("/metrics")
            assert resp.status_code == 500
            body = resp.json()
            assert body["error"]["code"] == "internal_error"

    def test_metrics_error_content_type_json(self):
        client, _ = _client(
            metrics_collector=None,
            litellm_bridge=None,
        )

        with patch(
            "aigateway_api.routes.generate_latest",
            side_effect=Exception("fail"),
        ):
            resp = client.get("/metrics")
            assert "application/json" in resp.headers["content-type"]
