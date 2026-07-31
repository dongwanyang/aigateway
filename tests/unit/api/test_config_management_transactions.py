from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml
from fastapi import APIRouter, HTTPException, Request

from aigateway_api import config_management_routes
from aigateway_api.config_security import config_revision
from aigateway_core.shared.config import ConfigManager


def _minimal_config() -> dict:
    return {
        "server": {"host": "0.0.0.0", "port": 8000},
        "plugins": [
            {
                "name": "pii_detector",
                "enabled": True,
                "depends_on": [],
                "config": {"strategy": "sanitize"},
            }
        ],
        "providers": {},
        "observability": {"log_level": "info"},
        "hot_reload": False,
        "debug_mode": False,
        "debug": {
            "frontend": False,
            "entry": False,
            "cache": False,
            "bridge": False,
            "plugins_enabled": False,
            "plugins": {
                "enabled": False,
                "per_plugin": {"pii_detector": False},
            },
        },
    }


def _request(
    manager: ConfigManager,
    path: str,
    body: dict,
    *,
    method: str = "PUT",
    revision: str | None = None,
) -> Request:
    payload = json.dumps(body).encode("utf-8")
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": payload, "more_body": False}

    headers = [(b"content-type", b"application/json")]
    if revision is not None:
        headers.append((b"if-match", f'"{revision}"'.encode("ascii")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "app": SimpleNamespace(state=SimpleNamespace(config_manager=manager)),
        },
        receive,
    )


@pytest.fixture
def manager(tmp_path, monkeypatch) -> ConfigManager:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_minimal_config(), sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_ENV", "production")
    return ConfigManager(str(path))


@pytest.mark.asyncio
async def test_plugin_toggle_uses_transaction_and_returns_revision(manager) -> None:
    response = await config_management_routes.update_plugins_config_transactional(
        _request(
            manager,
            "/admin/plugins-config",
            {"name": "pii_detector", "enabled": False},
        ),
        {},
    )

    with open(manager.config_path, encoding="utf-8") as file:
        persisted = yaml.safe_load(file)
    assert persisted["plugins"][0]["enabled"] is False
    assert manager.get("plugins")[0]["enabled"] is False
    assert response.headers["etag"] == f'"{config_revision(manager.config_path)}"'


@pytest.mark.asyncio
async def test_plugin_toggle_rejects_non_boolean_without_writing(manager) -> None:
    with open(manager.config_path, encoding="utf-8") as file:
        before = file.read()

    with pytest.raises(HTTPException) as exc_info:
        await config_management_routes.update_plugins_config_transactional(
            _request(
                manager,
                "/admin/plugins-config",
                {"name": "pii_detector", "enabled": "false"},
            ),
            {},
        )

    assert exc_info.value.status_code == 400
    with open(manager.config_path, encoding="utf-8") as file:
        assert file.read() == before


@pytest.mark.asyncio
async def test_plugin_toggle_rejects_stale_revision(manager) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await config_management_routes.update_plugins_config_transactional(
            _request(
                manager,
                "/admin/plugins-config",
                {"name": "pii_detector", "enabled": False},
                revision="stale",
            ),
            {},
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["error"]["code"] == "config_version_conflict"


@pytest.mark.asyncio
async def test_generation_plugin_toggle_updates_nested_gate(manager) -> None:
    response = await config_management_routes.update_plugins_config_transactional(
        _request(
            manager,
            "/admin/plugins-config",
            {"name": "draft_generator", "enabled": True},
        ),
        {},
    )

    with open(manager.config_path, encoding="utf-8") as file:
        persisted = yaml.safe_load(file)
    assert persisted["generation_optimization"]["enabled"] is True
    assert persisted["generation_optimization"]["draft_workflow"]["enabled"] is True
    assert json.loads(response.body)["data"]["enabled"] is True


@pytest.mark.asyncio
async def test_plugin_debug_transaction_updates_disk_and_runtime(manager) -> None:
    response = await config_management_routes.set_plugin_debug_transactional(
        "pii_detector",
        _request(
            manager,
            "/admin/plugins/pii_detector/debug",
            {"enabled": True},
            method="POST",
        ),
        {},
    )

    with open(manager.config_path, encoding="utf-8") as file:
        persisted = yaml.safe_load(file)
    assert persisted["debug"]["plugins"]["per_plugin"]["pii_detector"] is True
    assert manager.get("debug")["plugins"]["per_plugin"]["pii_detector"] is True
    assert json.loads(response.body)["revision"] == config_revision(manager.config_path)


@pytest.mark.asyncio
async def test_global_config_merges_partial_debug_and_applies_runtime(
    manager,
    monkeypatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(manager, "start_watching", lambda: calls.append("start"))
    monkeypatch.setattr(manager, "stop_watching", lambda: calls.append("stop"))
    monkeypatch.setattr(
        "aigateway_core.shared.logger.setup_logging",
        lambda *, log_level: calls.append(log_level),
    )

    response = await config_management_routes.update_global_config_transactional(
        _request(
            manager,
            "/admin/global-config",
            {
                "hot_reload": True,
                "debug_mode": True,
                "debug": {"entry": True, "plugins_enabled": True},
            },
        ),
        {},
    )

    with open(manager.config_path, encoding="utf-8") as file:
        persisted = yaml.safe_load(file)
    assert persisted["hot_reload"] is True
    assert persisted["debug_mode"] is True
    assert persisted["debug"]["entry"] is True
    assert persisted["debug"]["plugins"]["enabled"] is True
    assert persisted["debug"]["plugins_enabled"] is True
    assert persisted["debug"]["cache"] is False
    # Production environment mode intentionally keeps the effective runtime
    # debug flag disabled even when the persisted operator preference is true.
    assert manager.get("debug_mode") is False
    assert calls == ["start", "INFO"]
    assert json.loads(response.body)["data"]["hot_reload"] is True


@pytest.mark.asyncio
async def test_global_runtime_failure_rolls_back_file_and_runtime(
    manager,
    monkeypatch,
) -> None:
    calls: list[str] = []

    def fail_start() -> None:
        calls.append("start")
        raise RuntimeError("watchdog failed")

    monkeypatch.setattr(manager, "start_watching", fail_start)
    monkeypatch.setattr(manager, "stop_watching", lambda: calls.append("stop"))
    monkeypatch.setattr(
        "aigateway_core.shared.logger.setup_logging",
        lambda *, log_level: calls.append(log_level),
    )
    before_revision = config_revision(manager.config_path)

    with pytest.raises(HTTPException) as exc_info:
        await config_management_routes.update_global_config_transactional(
            _request(
                manager,
                "/admin/global-config",
                {"hot_reload": True},
            ),
            {},
        )

    with open(manager.config_path, encoding="utf-8") as file:
        persisted = yaml.safe_load(file)
    assert exc_info.value.status_code == 500
    assert persisted["hot_reload"] is False
    assert manager.get("hot_reload") is False
    assert config_revision(manager.config_path) == before_revision
    assert calls == ["start", "stop", "INFO"]


@pytest.mark.asyncio
async def test_global_config_rejects_unknown_or_non_boolean_fields(manager) -> None:
    for payload in (
        {"debug_mode": "true"},
        {"unexpected": True},
        {"debug": {"plugins": {"per_plugin": {"pii_detector": "true"}}}},
    ):
        with pytest.raises(HTTPException) as exc_info:
            await config_management_routes.update_global_config_transactional(
                _request(manager, "/admin/global-config", payload),
                {},
            )
        assert exc_info.value.status_code == 400


def test_install_replaces_conflicting_routes_and_is_idempotent() -> None:
    target = APIRouter()

    @target.put("/plugins-config")
    async def legacy_plugins():
        return None

    @target.put("/other")
    async def other():
        return None

    config_management_routes.install_config_management_routes(target)
    config_management_routes.install_config_management_routes(target)

    matches = [
        route
        for route in target.routes
        if getattr(route, "path", None) == "/plugins-config"
        and "PUT" in set(getattr(route, "methods", set()) or set())
    ]
    assert len(matches) == 1
    assert any(getattr(route, "path", None) == "/other" for route in target.routes)
