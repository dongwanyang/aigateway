from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from aigateway_api.auth_middleware import SESSION_COOKIE_NAME, authenticate, authenticate_admin
from aigateway_api.auth_routes import router as auth_router
from aigateway_api.browser_auth import BrowserAuthStore
from aigateway_api.routes import router as routes_router
from aigateway_core.shared.auth.sqlite_store import SQLiteStore


@pytest.mark.asyncio
async def test_browser_cookie_is_not_accepted_by_api_key_dependency(tmp_path):
    store = BrowserAuthStore(str(tmp_path / "auth.db"))
    user = store.provision_admin("admin", "temporary-admin-password")
    token = store.create_session(
        user["user_id"], ttl_seconds=3600, absolute_ttl_seconds=7200
    )

    request = MagicMock()
    request.headers = {}
    request.cookies = {SESSION_COOKIE_NAME: token}
    request.app.state.browser_auth_store = store

    with pytest.raises(HTTPException) as exc:
        await authenticate(request)
    assert exc.value.status_code == 401
    assert exc.value.detail["error"]["code"] == "unauthorized"


@pytest.mark.asyncio
async def test_force_reset_browser_session_is_blocked_on_admin_routes(tmp_path):
    store = BrowserAuthStore(str(tmp_path / "auth.db"))
    user = store.provision_admin("admin", "temporary-admin-password")
    token = store.create_session(
        user["user_id"], ttl_seconds=3600, absolute_ttl_seconds=7200
    )

    request = MagicMock()
    request.headers = {}
    request.cookies = {SESSION_COOKIE_NAME: token}
    request.app.state.browser_auth_store = store

    with pytest.raises(HTTPException) as exc:
        await authenticate_admin(request)
    assert exc.value.status_code == 403
    assert exc.value.detail["error"]["code"] == "password_change_required"


@pytest.mark.asyncio
async def test_session_cookie_uses_absolute_ttl_and_logout_matches_secure_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_INITIAL_ADMIN_PASSWORD", "temporary-admin-password")
    monkeypatch.setenv("AI_GATEWAY_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("AI_GATEWAY_SESSION_TTL_SECONDS", "5")
    monkeypatch.setenv("AI_GATEWAY_SESSION_ABSOLUTE_TTL_SECONDS", "60")
    monkeypatch.setenv("AI_GATEWAY_TRUST_PROXY_HEADERS", "true")

    app = FastAPI()
    app.state.key_store = SQLiteStore(str(tmp_path / "auth.db"))
    app.include_router(auth_router, prefix="/auth")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://testserver",
        headers={"x-forwarded-proto": "https"},
    ) as client:
        login = await client.post(
            "/auth/session",
            json={"username": "admin", "password": "temporary-admin-password"},
        )
        assert login.status_code == 200
        cookie = login.headers["set-cookie"].lower()
        assert "max-age=60" in cookie
        assert "secure" in cookie
        assert "httponly" in cookie

        logout = await client.delete("/auth/session")
        assert logout.status_code == 200
        clear_cookie = logout.headers["set-cookie"].lower()
        assert "secure" in clear_cookie
        assert "max-age=0" in clear_cookie


@pytest.mark.asyncio
async def test_forwarded_for_is_ignored_without_trusted_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_INITIAL_ADMIN_PASSWORD", "temporary-admin-password")
    monkeypatch.setenv("AI_GATEWAY_ADMIN_USERNAME", "admin")
    monkeypatch.delenv("AI_GATEWAY_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("AI_GATEWAY_TRUSTED_PROXY_IPS", raising=False)

    db_path = tmp_path / "auth.db"
    app = FastAPI()
    app.state.key_store = SQLiteStore(str(db_path))
    app.include_router(auth_router, prefix="/auth")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"x-forwarded-for": "203.0.113.10"},
    ) as client:
        response = await client.post(
            "/auth/session",
            json={"username": "admin", "password": "temporary-admin-password"},
        )
        assert response.status_code == 200

    with BrowserAuthStore(str(db_path))._connect() as conn:
        row = conn.execute("SELECT ip_address FROM browser_sessions LIMIT 1").fetchone()
    assert row["ip_address"] != "203.0.113.10"


@pytest.mark.asyncio
async def test_console_chat_blocks_force_reset_browser_session(tmp_path):
    store = BrowserAuthStore(str(tmp_path / "auth.db"))
    user = store.provision_admin("admin", "temporary-admin-password")
    token = store.create_session(
        user["user_id"], ttl_seconds=3600, absolute_ttl_seconds=7200
    )

    app = FastAPI()
    app.state.browser_auth_store = store
    app.include_router(routes_router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/admin/console/chat/completions",
            cookies={SESSION_COOKIE_NAME: token},
            json={"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["code"] == "password_change_required"


@pytest.mark.asyncio
async def test_console_chat_requires_server_side_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_GATEWAY_CONSOLE_CHAT_API_KEY", raising=False)
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)

    store = BrowserAuthStore(str(tmp_path / "auth.db"))
    user = store.provision_admin("admin", "temporary-admin-password")
    store.change_password(user["user_id"], "changed-admin-password")
    token = store.create_session(
        user["user_id"], ttl_seconds=3600, absolute_ttl_seconds=7200
    )

    app = FastAPI()
    app.state.browser_auth_store = store
    app.include_router(routes_router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/admin/console/chat/completions",
            cookies={SESSION_COOKIE_NAME: token},
            json={"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 503
    assert response.json()["detail"]["error"]["code"] == "console_chat_api_key_required"
