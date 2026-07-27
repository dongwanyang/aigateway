from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "aigateway-api/src"))
sys.path.insert(0, str(ROOT / "aigateway-core/src"))

from aigateway_api.auth_middleware import authenticate, authenticate_admin
from aigateway_api.auth_routes import router as auth_router
from aigateway_api.browser_auth import get_browser_auth_store
from aigateway_core.shared.auth.sqlite_store import SQLiteStore


@pytest.mark.asyncio
async def test_browser_session_cookie_is_not_machine_api_auth(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_INITIAL_ADMIN_PASSWORD", "temporary-admin-password")
    app = FastAPI()
    app.state.key_store = SQLiteStore(str(tmp_path / "auth.db"))
    app.include_router(auth_router, prefix="/auth")

    @app.get("/machine")
    async def machine_endpoint(_auth: Dict[str, Any] = Depends(authenticate)):
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        login = await client.post(
            "/auth/session",
            json={"username": "admin", "password": "temporary-admin-password"},
        )
        assert login.status_code == 200

        response = await client.get("/machine")

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "API key required"


@pytest.mark.asyncio
async def test_force_reset_session_is_blocked_from_admin_routes(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_INITIAL_ADMIN_PASSWORD", "temporary-admin-password")
    app = FastAPI()
    app.state.key_store = SQLiteStore(str(tmp_path / "auth.db"))
    app.include_router(auth_router, prefix="/auth")

    @app.get("/admin/protected")
    async def protected_admin(_auth: Dict[str, Any] = Depends(authenticate_admin)):
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        login = await client.post(
            "/auth/session",
            json={"username": "admin", "password": "temporary-admin-password"},
        )
        assert login.status_code == 200

        blocked = await client.get("/admin/protected")
        reset = await client.post(
            "/auth/reset-password",
            json={"new_password": "new-independent-admin-password"},
        )
        allowed = await client.get("/admin/protected")

    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "password_change_required"
    assert reset.status_code == 200
    assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_session_cookie_uses_absolute_ttl_and_secure_delete(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_INITIAL_ADMIN_PASSWORD", "temporary-admin-password")
    monkeypatch.setenv("AI_GATEWAY_SESSION_TTL_SECONDS", "10")
    monkeypatch.setenv("AI_GATEWAY_SESSION_ABSOLUTE_TTL_SECONDS", "60")
    app = FastAPI()
    app.state.key_store = SQLiteStore(str(tmp_path / "auth.db"))
    app.include_router(auth_router, prefix="/auth")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as client:
        login = await client.post(
            "/auth/session",
            json={"username": "admin", "password": "temporary-admin-password"},
        )
        logout = await client.delete("/auth/session")

    login_cookie = login.headers["set-cookie"].lower()
    logout_cookie = logout.headers["set-cookie"].lower()
    assert re.search(r"max-age=60(?:;|$)", login_cookie)
    assert "secure" in login_cookie
    assert "secure" in logout_cookie


@pytest.mark.asyncio
async def test_x_forwarded_for_requires_trusted_proxy(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_INITIAL_ADMIN_PASSWORD", "temporary-admin-password")
    monkeypatch.delenv("AI_GATEWAY_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("AI_GATEWAY_TRUSTED_PROXY_IPS", raising=False)
    app = FastAPI()
    app.state.key_store = SQLiteStore(str(tmp_path / "auth.db"))
    app.include_router(auth_router, prefix="/auth")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/auth/session",
            headers={"x-forwarded-for": "203.0.113.10"},
            json={"username": "admin", "password": "temporary-admin-password"},
        )
    assert response.status_code == 200

    store = get_browser_auth_store(type("Req", (), {"app": app})())
    with store._connect() as conn:
        row = conn.execute("SELECT ip_address FROM browser_sessions LIMIT 1").fetchone()
    assert row is not None
    assert row["ip_address"] != "203.0.113.10"
