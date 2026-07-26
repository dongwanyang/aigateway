"""Tests for runtime capability discovery used by split images and the UI."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aigateway_api import capabilities


class _ConfigManager:
    def __init__(self, values: dict):
        self._values = values

    def get(self, key: str, default=None):
        return self._values.get(key, default)


def test_runtime_profile_reports_optional_features_as_unavailable(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_IMAGE_PROFILE", "runtime")
    state = SimpleNamespace(
        cache_manager=SimpleNamespace(_qdrant_client=None),
        config_manager=_ConfigManager({"code_rag": {"enabled": True}}),
    )

    result = capabilities.detect_runtime_capabilities(
        state,
        package_probe=lambda _name: False,
        executable_probe=lambda _name: None,
        cuda_probe=lambda: False,
    )

    assert result["profile"] == "runtime"
    assert result["capabilities"]["core"]["available"] is True
    assert result["capabilities"]["rag"]["installed"] is False
    assert result["capabilities"]["rag"]["available"] is False
    assert result["capabilities"]["vision"]["available"] is False
    assert result["capabilities"]["gpu"]["available"] is False
    assert "--add rag" in result["capabilities"]["rag"]["install_command"]


def test_full_profile_reports_installed_features_and_cuda(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_IMAGE_PROFILE", "full")
    monkeypatch.setattr(capabilities.os.path, "isfile", lambda _path: True)
    state = SimpleNamespace(
        cache_manager=SimpleNamespace(_qdrant_client=object()),
        config_manager=_ConfigManager({"code_rag": {"enabled": True}}),
    )

    result = capabilities.detect_runtime_capabilities(
        state,
        package_probe=lambda _name: True,
        executable_probe=lambda name: f"/usr/bin/{name}",
        cuda_probe=lambda: True,
    )

    detected = result["capabilities"]
    assert result["profile"] == "full"
    for name in ("core", "rag", "code_rag", "vision", "upscaling", "gpu"):
        assert detected[name]["installed"] is True
        assert detected[name]["available"] is True


def test_installed_rag_reports_configuration_failure():
    state = SimpleNamespace(
        cache_manager=SimpleNamespace(_qdrant_client=None),
        config_manager=_ConfigManager({"code_rag": {"enabled": True}}),
    )

    result = capabilities.detect_runtime_capabilities(
        state,
        package_probe=lambda _name: True,
        executable_probe=lambda name: f"/usr/bin/{name}",
        cuda_probe=lambda: False,
    )

    rag = result["capabilities"]["rag"]
    assert rag["installed"] is True
    assert rag["configured"] is False
    assert rag["available"] is False
    assert rag["reason"] == "Qdrant 未连接"


def test_admin_capabilities_endpoint_uses_standard_envelope(monkeypatch):
    from aigateway_api import admin_routes

    expected = {"profile": "runtime", "capabilities": {"core": {"available": True}}}
    monkeypatch.setattr(
        capabilities,
        "detect_runtime_capabilities",
        lambda _state: expected,
    )

    app = FastAPI()
    app.include_router(admin_routes.router, prefix="/admin")
    app.dependency_overrides[admin_routes.authenticate_admin] = lambda: {}

    response = TestClient(app).get("/admin/capabilities")
    assert response.status_code == 200
    assert response.json() == {"data": expected, "message": "success"}
