from __future__ import annotations

import pytest
from aigateway_api.auth_routes import router as auth_router
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_auth_login_works_without_config_file(tmp_path, monkeypatch):
    missing_config = tmp_path / "missing-config.yaml"
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", str(missing_config))
    monkeypatch.setenv(
        "AI_GATEWAY_INITIAL_ADMIN_PASSWORD",
        "temporary-admin-password",
    )
    monkeypatch.setenv("AI_GATEWAY_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("AI_GATEWAY_PASSWORD_PBKDF2_ITERATIONS", "1000")
    monkeypatch.setenv("AI_GATEWAY_AUTH_DATABASE_TIMEOUT_SECONDS", "2")
    monkeypatch.delenv("AI_GATEWAY_ADMIN_USER_ID", raising=False)
    monkeypatch.delenv("AI_GATEWAY_SESSION_TTL_SECONDS", raising=False)
    monkeypatch.delenv(
        "AI_GATEWAY_SESSION_ABSOLUTE_TTL_SECONDS",
        raising=False,
    )

    app = FastAPI()
    app.state.key_store = type(
        "KeyStoreStub",
        (),
        {"db_path": str(tmp_path / "auth.db")},
    )()
    app.include_router(auth_router, prefix="/auth")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/auth/session",
            json={
                "username": "admin",
                "password": "temporary-admin-password",
            },
        )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "authenticated": True,
        "key_prefix": "admin",
        "scopes": ["admin", "chat", "embedding"],
        "force_reset": True,
    }
    assert "aigateway_session=" in response.headers["set-cookie"]
