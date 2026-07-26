"""Regression coverage for the production P1 security/reliability fixes."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parents[3]  # tests/unit/integration/ → aigateway/ (parents[3])
sys.path.insert(0, str(ROOT / "aigateway-api/src"))
sys.path.insert(0, str(ROOT / "aigateway-core/src"))

from aigateway_api.admin_routes import _rag_document_identity
from aigateway_api.auth_routes import router as auth_router
from aigateway_api.auth_middleware import authenticate_admin
from aigateway_core.prefix.cache.cache_manager import CacheManager
from aigateway_core.route.streaming.sse import SSEGenerator
from aigateway_core.shared.auth.sqlite_store import SQLiteStore
from aigateway_core.shared.exceptions import AuthError


@pytest.mark.asyncio
async def test_key_expiry_rotation_and_scopes_are_enforced(tmp_path: Path):
    store = SQLiteStore(str(tmp_path / "auth.db"))

    async def inline_db(fn):
        return fn()

    store._db = inline_db
    created = await store.create(
        "admin-user",
        scopes=["admin", "chat", "embedding"],
        expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
    )

    validated = await store.validate(created["key"])
    assert validated["scopes"] == ["admin", "chat", "embedding"]
    assert validated["last_used_at"]

    replacement = await store.rotate(created["id"])
    with pytest.raises(AuthError, match="revoked"):
        await store.validate(created["key"])
    replacement_data = await store.validate(replacement["key"])
    assert replacement_data["scopes"] == ["admin", "chat", "embedding"]

    old_hash = store._lookup_by_id(created["id"])[0]
    old = dict(store._api_key_row(old_hash))
    assert old["revoked_at"]
    assert old["rotated_at"]


@pytest.mark.asyncio
async def test_expired_key_is_rejected(tmp_path: Path):
    store = SQLiteStore(str(tmp_path / "auth.db"))

    async def inline_db(fn):
        return fn()

    store._db = inline_db
    created = await store.create(
        "expired-user",
        expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    )
    with pytest.raises(AuthError, match="expired"):
        await store.validate(created["key"])


@pytest.mark.asyncio
async def test_admin_auth_requires_scope_not_is_admin_flag():
    key_store = AsyncMock()
    key_store.validate.return_value = {
        "key_id": "legacy-admin",
        "is_admin": True,
        "scopes": ["chat"],
    }
    request = MagicMock()
    request.headers = {"authorization": "Bearer key", "x-api-key": ""}
    request.cookies = {}
    request.app.state.key_store = key_store

    with pytest.raises(HTTPException) as exc:
        await authenticate_admin(request)
    assert exc.value.status_code == 403
    assert exc.value.detail["error"]["code"] == "insufficient_scope"


@pytest.mark.asyncio
async def test_browser_session_cookie_is_httponly_and_secret_not_returned():
    app = FastAPI()
    app.state.key_store = AsyncMock()
    app.state.key_store.validate.return_value = {
        "key_prefix": "gw-test1",
        "scopes": ["admin", "chat", "embedding"],
    }
    app.state.key_store.check_is_default.return_value = False
    app.include_router(auth_router, prefix="/auth")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/auth/session", json={"api_key": "gw-test1-secret"}
        )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "gw-test1-secret" not in response.text


@pytest.mark.asyncio
async def test_default_admin_reset_survives_session_refresh_and_reseed(tmp_path: Path):
    old_key = "gw-default-admin-key-1234567890"
    new_key = "gw-replacement-admin-key-0987654321"
    config = [{
        "key": old_key,
        "user_id": "admin",
        "scopes": ["admin", "chat", "embedding"],
        "group": "admin-team",
    }]
    store = SQLiteStore(str(tmp_path / "session-auth.db"))
    # Keep DB operations deterministic within this in-process ASGI test.
    async def inline_db(fn):
        return fn()

    store._db = inline_db
    await store.seed_from_config(config)

    app = FastAPI()
    app.state.key_store = store
    app.include_router(auth_router, prefix="/auth")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        login = await client.post("/auth/session", json={"api_key": old_key})
        assert login.status_code == 200
        assert login.json()["data"]["force_reset"] is True

        refreshed = await client.get("/auth/session")
        assert refreshed.json()["data"]["force_reset"] is True

        reset = await client.post(
            "/auth/reset-password",
            json={"new_api_key": new_key},
        )
        assert reset.status_code == 200
        assert reset.json()["data"]["new_api_key"] == new_key

        refreshed = await client.get("/auth/session")
        assert refreshed.status_code == 200
        assert refreshed.json()["data"]["authenticated"] is True
        assert refreshed.json()["data"]["force_reset"] is False

    with pytest.raises(AuthError, match="revoked"):
        await store.validate(old_key)
    assert (await store.validate(new_key))["scopes"] == [
        "admin", "chat", "embedding",
    ]

    # Startup config seeding must not resurrect the revoked bootstrap key.
    await store.seed_from_config(config)
    with pytest.raises(AuthError, match="revoked"):
        await store.validate(old_key)


class _ClosableUpstream:
    def __init__(self):
        self.step = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.step == 0:
            self.step += 1
            return {"delta": "first"}
        await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_sse_cancellation_closes_upstream_without_done_frame():
    upstream = _ClosableUpstream()
    stream = SSEGenerator(upstream).generate()
    assert await stream.__anext__() == 'data: {"delta": "first"}\n\n'

    pending = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert upstream.closed is True


def test_cache_pipeline_version_isolation():
    base = {
        "normalized_prompt": "same",
        "model": "gpt-4o",
        "pipeline_kind": "understanding",
    }
    assert CacheManager.generate_cache_key(**base, pipeline_version="1") != (
        CacheManager.generate_cache_key(**base, pipeline_version="2")
    )


def test_rag_document_identity_is_idempotent_and_versioned():
    args = {
        "chunk_strategy": "fixed_size",
        "chunk_size": 512,
        "chunk_overlap": 64,
    }
    first = _rag_document_identity("same content", **args)
    second = _rag_document_identity("same content", **args)
    changed = _rag_document_identity("changed content", **args)
    assert first == second
    assert first != changed
    assert len(first[0]) == 64
    assert first[1].startswith("doc_")


def test_frontend_and_nginx_security_contracts():
    root = Path(__file__).resolve().parents[3]  # tests/unit/integration/ → aigateway/
    client = (root / "control-panel/src/api/client.ts").read_text()
    nginx = (root / "control-panel/nginx.conf").read_text()
    prod_nginx = (root / "control-panel/nginx.prod.conf").read_text()
    workflow = (root / ".github/workflows/security.yml").read_text()
    benchmark_workflow = (root / ".github/workflows/benchmark.yml").read_text()

    assert "localStorage.setItem('aigateway_api_key'" not in client
    assert "localStorage.getItem('aigateway_api_key'" not in client
    assert "/auth/session" in client
    assert "Content-Security-Policy" in nginx
    assert "return 308 https://$host$request_uri" in prod_nginx
    assert "gitleaks/gitleaks-action@v3" in workflow
    assert "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in workflow
    assert "aquasecurity/trivy-action@v0.36.0" in workflow
    assert "trivyignores: .trivyignore.yaml" in workflow
    # security.yml has pip install + pip-audit steps (no path filters like "inputs:")
    assert "pip install -e aigateway-api" in workflow
    assert "pip install -e aigateway-core" in workflow
    assert "pip install -e aigateway-cli" in workflow
    assert "gh-action-pip-audit" in workflow
    assert "uvicorn aigateway_api.main:create_app" in benchmark_workflow
    assert "uvicorn src.aigateway_api.main:create_app" not in benchmark_workflow
    assert "'.github/workflows/benchmark.yml'" in benchmark_workflow
    assert "!inputs.with_media" in benchmark_workflow
    assert "IFS=',' read -ra scenario_args" in benchmark_workflow
    assert "BENCHMARK_LIVE:" in benchmark_workflow
    assert "--dry-run" in benchmark_workflow
    assert "AI_GATEWAY_REDIS_URL: redis://localhost:6379/0" in benchmark_workflow
    assert 'test -f "$report"' in benchmark_workflow
