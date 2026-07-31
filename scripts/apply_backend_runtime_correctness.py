#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"expected block not found in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# 1) Never expose exception text in any 5xx response.
main_path = "aigateway-api/src/aigateway_api/main.py"
replace_once(
    main_path,
    '''        body = {"error": {"code": code, "message": msg}}

        # 5xx 错误不能在 message 中回显原始异常。旧实现只脱敏 detail，
        # 但 message 仍会泄露 API key、连接串和服务器路径，使脱敏形同虚设。
        # 对外使用固定文案，诊断信息仅放在经过脱敏的 detail 中。
        if status >= 500:
            body["error"]["message"] = "Internal Server Error"
            body["error"]["detail"] = f"{type(exc).__name__}: {_redact_5xx_msg(msg)}"
''',
    '''        body = {"error": {"code": code, "message": msg}}

        # 5xx diagnostics belong in server logs only.  Even a carefully redacted
        # exception string can contain provider-specific credentials or internal
        # topology that a generic regular expression does not recognize.
        if status >= 500:
            logger.error(
                "GatewayError (request_id=%s, type=%s): %s",
                request_id,
                type(exc).__name__,
                _redact_5xx_msg(msg),
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            body = {
                "error": {
                    "code": code,
                    "message": "Internal Server Error",
                }
            }
''',
)
replace_once(
    main_path,
    '''        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            body = detail
        else:
            body = {"error": {"code": "internal_error", "message": str(detail) if detail else "Internal error"}}

        request_id = _get_request_id(request)
''',
    '''        detail = exc.detail
        request_id = _get_request_id(request)
        if exc.status_code >= 500:
            error_code = "internal_error"
            if isinstance(detail, dict):
                nested_error = detail.get("error")
                if isinstance(nested_error, dict):
                    candidate = nested_error.get("code")
                    if isinstance(candidate, str) and candidate:
                        error_code = candidate
            logger.error(
                "HTTPException (request_id=%s, status=%s): %r",
                request_id,
                exc.status_code,
                detail,
            )
            body = {
                "error": {
                    "code": error_code,
                    "message": "Internal Server Error",
                }
            }
        elif isinstance(detail, dict) and "error" in detail:
            body = detail
        else:
            body = {"error": {"code": "internal_error", "message": str(detail) if detail else "Internal error"}}

''',
)
replace_once(
    main_path,
    '''            content={
                "error": {
                    "code": "internal_error",
                    "message": "Internal Server Error",
                    "detail": f"{type(exc).__name__}: {redacted_msg}",
                }
            },
''',
    '''            content={
                "error": {
                    "code": "internal_error",
                    "message": "Internal Server Error",
                }
            },
''',
)

# 2) Fix config locking, strict management payloads, quota response, and metrics.
admin_path = "aigateway-api/src/aigateway_api/admin_routes.py"
replace_once(
    admin_path,
    '''def _read_yaml(config_path: str) -> dict[str, Any]:
    """Read the small runtime YAML file from disk."""
    import yaml

    with open(config_path, encoding="utf-8") as file:
        return yaml.safe_load(file) or {}
''',
    '''def _read_yaml(config_path: str) -> dict[str, Any]:
    """Read runtime YAML while cooperating with in-place bind-mount writes."""
    import fcntl

    import yaml

    with open(config_path, encoding="utf-8") as file:
        fcntl.flock(file.fileno(), fcntl.LOCK_SH)
        try:
            return yaml.safe_load(file) or {}
        finally:
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)
''',
)
replace_once(
    admin_path,
    '''def _flocked_inplace_write(config_path: str, file_config: dict[str, Any]) -> None:
    """flocked 原地写回退(用于 os.replace 不可用的 bind-mount 场景)。

    排它锁保证 Watchdog 的 _load_yaml(共享锁)不会读到半截 YAML。
    """
    import fcntl

    import yaml

    with open(config_path, "w", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yaml.dump(file_config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
''',
    '''def _flocked_inplace_write(config_path: str, file_config: dict[str, Any]) -> None:
    """Update a bind-mounted YAML file without truncating before the lock."""
    import fcntl
    import os

    import yaml

    # Opening with ``w`` truncates before flock() is acquired.  Use r+ so every
    # cooperating reader sees either the old complete document or the new one.
    with open(config_path, "r+", encoding="utf-8") as file:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        try:
            file.seek(0)
            file.truncate(0)
            yaml.dump(
                file_config,
                file,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )
            file.flush()
            os.fsync(file.fileno())
        finally:
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)
''',
)
replace_once(
    admin_path,
    '''    await key_store.set_api_key(kh, updated_fields)

    logger.info("API Key 配额已更新: key_id=%s, fields=%s", key_id, list(updated_fields.keys()))

    return {
        "data": {
            "id": key_id,
            "user_id": data.get("user_id", ""),
            "quotas": {
                "daily_tokens_limit": int(data.get("daily_tokens_limit", _get_auth_defaults()["daily_tokens"])),
                "monthly_cost_limit": float(data.get("monthly_cost_limit", _get_auth_defaults()["monthly_cost"])),
                "rate_limit_rpm": int(data.get("rate_limit_rpm", _get_auth_defaults()["rate_limit_rpm"])),
                "rate_limit_tpm": int(data.get("rate_limit_tpm", _get_auth_defaults()["rate_limit_tpm"])),
            },
        },
''',
    '''    await key_store.set_api_key(kh, updated_fields)
    updated_data = await key_store.get_api_key(kh)
    if not updated_data:
        updated_data = {**data, **updated_fields}

    logger.info("API Key 配额已更新: key_id=%s, fields=%s", key_id, list(updated_fields.keys()))

    return {
        "data": {
            "id": key_id,
            "user_id": updated_data.get("user_id", ""),
            "quotas": {
                "daily_tokens_limit": int(updated_data.get("daily_tokens_limit", _get_auth_defaults()["daily_tokens"])),
                "monthly_cost_limit": float(updated_data.get("monthly_cost_limit", _get_auth_defaults()["monthly_cost"])),
                "rate_limit_rpm": int(updated_data.get("rate_limit_rpm", _get_auth_defaults()["rate_limit_rpm"])),
                "rate_limit_tpm": int(updated_data.get("rate_limit_tpm", _get_auth_defaults()["rate_limit_tpm"])),
            },
        },
''',
)
replace_once(
    admin_path,
    '''    from pydantic import BaseModel, Field

    class PluginToggleRequest(BaseModel):
        name: str = Field(..., min_length=1)
        enabled: bool

    try:
        # 解析请求体
        raw = await request.json()
        name = raw.get("name", "")
        enabled = raw.get("enabled", True)
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": "Invalid request body"}})

    if not name:
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": "Plugin name is required"}})
''',
    '''    from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

    class PluginToggleRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")

        name: str = Field(..., min_length=1)
        enabled: StrictBool

    try:
        payload = PluginToggleRequest.model_validate(await request.json())
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "validation_error",
                    "message": "name must be a non-empty string and enabled must be a boolean",
                }
            },
        ) from exc

    name = payload.name
    enabled = bool(payload.enabled)
''',
)
replace_once(
    admin_path,
    '''    body = await request.json()
    enabled = bool(body.get("enabled", False))
''',
    '''    from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError

    class PluginDebugRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")

        enabled: StrictBool

    try:
        payload = PluginDebugRequest.model_validate(await request.json())
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "validation_error",
                    "message": "enabled must be a boolean",
                }
            },
        ) from exc
    enabled = bool(payload.enabled)
''',
)
replace_once(
    admin_path,
    '''    raw = await request.json()
    # hot_reload / debug_mode 缺失时保留当前值,而非默认 False
    # (避免只传 debug 段的调用意外停掉 Watchdog 或重置日志级别)。
    cur_hot_reload = bool(config_manager.get("hot_reload", False)) if config_manager else False
    cur_debug_mode = bool(config_manager.get("debug_mode", False)) if config_manager else False
    hot_reload = bool(raw.get("hot_reload", cur_hot_reload))
    debug_mode = bool(raw.get("debug_mode", cur_debug_mode))
    debug_section = raw.get("debug")  # None 表示不改;dict 表示整段覆盖
''',
    '''    from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError

    class GlobalConfigUpdate(BaseModel):
        model_config = ConfigDict(extra="forbid")

        hot_reload: StrictBool | None = None
        debug_mode: StrictBool | None = None
        debug: dict[str, Any] | None = None

    try:
        payload = GlobalConfigUpdate.model_validate(await request.json())
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "validation_error",
                    "message": "Invalid global configuration payload",
                }
            },
        ) from exc

    # hot_reload / debug_mode 缺失时保留当前值,而非默认 False
    # (避免只传 debug 段的调用意外停掉 Watchdog 或重置日志级别)。
    cur_hot_reload = bool(config_manager.get("hot_reload", False)) if config_manager else False
    cur_debug_mode = bool(config_manager.get("debug_mode", False)) if config_manager else False
    hot_reload = cur_hot_reload if payload.hot_reload is None else bool(payload.hot_reload)
    debug_mode = cur_debug_mode if payload.debug_mode is None else bool(payload.debug_mode)
    debug_section = payload.debug  # None 表示不改;dict 表示整段覆盖
''',
)
replace_once(
    admin_path,
    '''    # 收集 Prometheus 指标
    prom_samples: dict[str, Any] = {}
    try:
        from prometheus_client import generate_latest
        # 单 worker 模式：使用 MetricsCollector 持有的 registry
        if metrics_collector and metrics_collector._registry is not None:
            raw = generate_latest(metrics_collector._registry).decode("utf-8")
        else:
            raw = ""
        for line in raw.split("\\n"):
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^(\\w+)\\{?([^}]*)\\}?\\s+(.+)$", line)
            if m:
                name, labels_str, value = m.groups()
                labels = {}
                if labels_str:
                    for pair in labels_str.split(","):
                        kv = pair.split("=")
                        if len(kv) == 2:
                            labels[kv[0]] = kv[1].strip('"')
                prom_samples[name] = {"labels": labels, "value": float(value)}
    except Exception as exc:
        logger.warning("Failed to collect Prometheus metrics: %s", exc)
''',
    '''    # Preserve every labelled Prometheus series.  ``prometheus`` remains a
    # backward-compatible latest-sample view; new consumers should use
    # ``prometheus_series`` to avoid losing all but the final label set.
    prom_series: dict[str, list[dict[str, Any]]] = {}
    try:
        import math

        from prometheus_client import generate_latest
        from prometheus_client.parser import text_string_to_metric_families

        if metrics_collector and metrics_collector._registry is not None:
            raw = generate_latest(metrics_collector._registry).decode("utf-8")
        else:
            raw = ""
        for family in text_string_to_metric_families(raw):
            for sample in family.samples:
                value = float(sample.value)
                if not math.isfinite(value):
                    continue
                prom_series.setdefault(sample.name, []).append(
                    {
                        "labels": dict(sample.labels),
                        "value": value,
                    }
                )
    except Exception as exc:
        logger.warning("Failed to collect Prometheus metrics: %s", exc)
    prom_samples = {
        name: samples[-1]
        for name, samples in prom_series.items()
        if samples
    }
''',
)
replace_once(
    admin_path,
    '''            "prometheus": prom_samples,
            "keys": key_stats,
''',
    '''            "prometheus": prom_samples,
            "prometheus_series": prom_series,
            "keys": key_stats,
''',
)

# 3) Make ComfyUI probing degrade per endpoint instead of all-or-nothing.
local_generation_path = "aigateway-api/src/aigateway_api/local_generation.py"
replace_once(
    local_generation_path,
    '''        "configuration_errors": config_errors,
        "error": config_errors[0] if config_errors else None,
    }
''',
    '''        "configuration_errors": config_errors,
        "endpoint_errors": {},
        "error": config_errors[0] if config_errors else None,
    }
''',
)
replace_once(
    local_generation_path,
    '''        async with httpx.AsyncClient(timeout=timeout) as client:
            stats_response, object_response, queue_response = await asyncio.gather(
                client.get(f"{server_url}/system_stats"),
                client.get(f"{server_url}/object_info"),
                client.get(f"{server_url}/queue"),
            )
        stats_response.raise_for_status()
        object_response.raise_for_status()
        queue_response.raise_for_status()
        stats = stats_response.json()
        objects = object_response.json()
        queue = queue_response.json()
        devices = stats.get("devices", []) if isinstance(stats, dict) else []
        result.update(
            {
                "available": True,
                "gpu": devices[0] if devices else None,
                "queue": {
                    "running": len(queue.get("queue_running", [])),
                    "pending": len(queue.get("queue_pending", [])),
                },
                "available_nodes": sorted(objects) if isinstance(objects, dict) else [],
                "error": config_errors[0] if config_errors else None,
            }
        )
''',
    '''        async with httpx.AsyncClient(timeout=timeout) as client:
            responses = await asyncio.gather(
                client.get(f"{server_url}/system_stats"),
                client.get(f"{server_url}/object_info"),
                client.get(f"{server_url}/queue"),
                return_exceptions=True,
            )

        payloads: dict[str, Any] = {}
        endpoint_errors: dict[str, str] = {}
        for endpoint, response in zip(
            ("system_stats", "object_info", "queue"),
            responses,
            strict=True,
        ):
            if isinstance(response, BaseException):
                endpoint_errors[endpoint] = type(response).__name__
                continue
            try:
                response.raise_for_status()
                payloads[endpoint] = response.json()
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                endpoint_errors[endpoint] = type(exc).__name__

        stats = payloads.get("system_stats")
        objects = payloads.get("object_info")
        queue = payloads.get("queue")
        devices = stats.get("devices", []) if isinstance(stats, dict) else []
        queue_view = None
        if isinstance(queue, dict):
            queue_view = {
                "running": len(queue.get("queue_running", [])),
                "pending": len(queue.get("queue_pending", [])),
            }
        result.update(
            {
                "available": bool(payloads),
                "gpu": devices[0] if devices else None,
                "queue": queue_view,
                "available_nodes": sorted(objects) if isinstance(objects, dict) else [],
                "endpoint_errors": endpoint_errors,
                "error": (
                    config_errors[0]
                    if config_errors
                    else (next(iter(endpoint_errors.values())) if not payloads and endpoint_errors else None)
                ),
            }
        )
''',
)

# 4) Add focused regressions.
test_path = Path("tests/unit/test_runtime_correctness_regressions.py")
test_path.write_text(
    '''from __future__ import annotations

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
''',
    encoding="utf-8",
)
