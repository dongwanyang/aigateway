"""Admin debug endpoints —— GET /admin/config/debug 返回 5 维度配置."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

import httpx
import pytest
from aigateway_core.shared.debug_config import DebugConfig
from fastapi import FastAPI


@pytest.mark.asyncio
async def test_get_debug_config_endpoint_returns_all_dims(monkeypatch):
    """GET /admin/config/debug 应返回所有维度键（结构验证）。"""
    from aigateway_api import admin_routes
    from aigateway_core.shared import debug_config

    expected = DebugConfig(
        frontend=True,
        entry=False,
        cache=True,
        bridge=False,
        plugins_enabled=True,
        per_plugin={"rag": True},
    )
    monkeypatch.setattr(debug_config, "get_debug_config", lambda: expected)

    app = FastAPI()
    app.include_router(admin_routes.router, prefix="/admin")

    async def fake_auth():
        return {"role": "admin"}

    app.dependency_overrides[admin_routes.authenticate_admin] = fake_auth
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        resp = await client.get("/admin/config/debug")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "data": {
            "frontend": True,
            "entry": False,
            "cache": True,
            "bridge": False,
            "plugins_enabled": True,
            "per_plugin": {"rag": True},
        },
        "message": "success",
    }
