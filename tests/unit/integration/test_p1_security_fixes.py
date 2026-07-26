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
from aigateway_api.auth_routes import router as auth_router, _hash_key
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
async def test_bootstrap_credentials_prefill_and_account_login(monkeypatch):
    initial_key = "gw-generated-admin-key-1234567890"
    monkeypatch.setenv("ADMIN_API_KEY", initial_key)
    monkeypatch.setenv("AI_GATEWAY_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("AI_GATEWAY_PREFILL_INITIAL_CREDENTIALS", "true")

    app = FastAPI()
    app.state.key_store = AsyncMock()
    app.state.key_store.validate.return_value = {
        "key_prefix": "gw-gener",
        "scopes": ["admin", "chat", "embedding"],
    }
    app.state.key_store.check_is_default.return_value = True
    app.include_router(auth_router, prefix="/auth")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        bootstrap = await client.get("/auth/bootstrap")
        assert bootstrap.status_code == 200
        assert bootstrap.headers["cache-control"] == "no-store, max-age=0"
        assert bootstrap.json()["data"] == {
            "available": True,
            "username": "admin",
            "initial_password": initial_key,
        }

        login = await client.post(
            "/auth/session",
            json={"username": "admin", "password": initial_key},
        )
        assert login.status_code == 200
        assert login.json()["data"]["force_reset"] is True

        wrong_user = await client.post(
            "/auth/session",
            json={"username": "root", "password": initial_key},
        )
        assert wrong_user.status_code == 401


@pytest.mark.asyncio
async def test_bootstrap_credentials_can_be_disabled(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "gw-generated-admin-key-1234567890")
    monkeypatch.setenv("AI_GATEWAY_PREFILL_INITIAL_CREDENTIALS", "false")
    app = FastAPI()
    app.state.key_store = AsyncMock()
    app.include_router(auth_router, prefix="/auth")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/auth/bootstrap")
    assert response.json()["data"] == {"available": False}
    app.state.key_store.validate.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_credentials_support_legacy_config_key(monkeypatch):
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    monkeypatch.setenv("AI_GATEWAY_PREFILL_INITIAL_CREDENTIALS", "true")
    legacy_key = "gw-legacy-config-admin-key-123456"
    app = FastAPI()
    app.state.key_store = AsyncMock()
    app.state.key_store.validate.return_value = {
        "key_prefix": "gw-legac",
        "scopes": ["admin"],
    }
    app.state.key_store.check_is_default.return_value = True
    app.state.config_manager = MagicMock()
    app.state.config_manager.get.return_value = {
        "api_keys": [{
            "key": legacy_key,
            "user_id": "admin",
            "scopes": ["admin", "chat"],
        }]
    }
    app.include_router(auth_router, prefix="/auth")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/auth/bootstrap")
    assert response.json()["data"]["initial_password"] == legacy_key


@pytest.mark.asyncio
async def test_account_login_rejects_non_admin_key_as_password(monkeypatch):
    """A valid non-admin API key submitted as a password must not log in.

    This is the gate separating account login from any-API-key login: only keys
    with the admin scope may authenticate via the username/password path.
    """
    monkeypatch.setenv("AI_GATEWAY_ADMIN_USERNAME", "admin")
    app = FastAPI()
    app.state.key_store = AsyncMock()
    app.state.key_store.validate.return_value = {
        "key_prefix": "gw-chat-",
        "scopes": ["chat"],
    }
    app.include_router(auth_router, prefix="/auth")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/auth/session",
            json={"username": "admin", "password": "gw-chat-only-key"},
        )
    assert response.status_code == 401
    assert response.json()["detail"]["error"]["message"] == "Invalid username or password"


@pytest.mark.asyncio
async def test_account_login_with_non_ascii_username_returns_401_not_500(monkeypatch):
    """secrets.compare_digest raises on non-ASCII str; the handler must not 500."""
    monkeypatch.setenv("AI_GATEWAY_ADMIN_USERNAME", "admin")
    app = FastAPI()
    app.state.key_store = AsyncMock()
    app.state.key_store.validate.return_value = None
    app.include_router(auth_router, prefix="/auth")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/auth/session",
            json={"username": "管理员", "password": "anything"},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_account_login_api_key_takes_precedence_over_username(monkeypatch):
    """When both api_key and username/password are sent, api_key wins and no
    admin-scope check is applied (legacy API-key login path)."""
    monkeypatch.setenv("AI_GATEWAY_ADMIN_USERNAME", "admin")
    app = FastAPI()
    app.state.key_store = AsyncMock()
    # A chat-only key would be rejected by the account-login scope gate, but
    # because api_key is present, account_login stays False and it succeeds.
    app.state.key_store.validate.return_value = {
        "key_prefix": "gw-chat-",
        "scopes": ["chat"],
    }
    app.state.key_store.check_is_default.return_value = False
    app.include_router(auth_router, prefix="/auth")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/auth/session",
            json={
                "api_key": "gw-chat-only-key",
                "username": "admin",
                "password": "ignored",
            },
        )
    assert response.status_code == 200
    assert response.json()["data"]["scopes"] == ["chat"]


@pytest.mark.asyncio
async def test_bootstrap_credentials_disabled_by_default(monkeypatch):
    """Without the opt-in flag, bootstrap never exposes credentials even if a
    default key exists — the safe default for any deployment."""
    monkeypatch.setenv("ADMIN_API_KEY", "gw-generated-admin-key-1234567890")
    monkeypatch.delenv("AI_GATEWAY_PREFILL_INITIAL_CREDENTIALS", raising=False)
    app = FastAPI()
    app.state.key_store = AsyncMock()
    app.state.key_store.check_is_default.return_value = True
    app.include_router(auth_router, prefix="/auth")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/auth/bootstrap")
    assert response.json()["data"] == {"available": False}
    app.state.key_store.check_is_default.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_reset_scrubs_admin_key_from_env(tmp_path: Path, monkeypatch):
    """Force-reset must delete the installer-seeded ADMIN_API_KEY from .env.

    Otherwise a future DB wipe would reseed the *revoked* key back to active,
    resurrecting a credential the operator believed dead. The new key is NOT
    written back — runtime secrets stay out of the plaintext config.
    """
    old_key = "gw-default-admin-key-1234567890"
    new_key = "gw-replacement-admin-key-0987654321"
    config = [{
        "key": old_key, "user_id": "admin",
        "scopes": ["admin", "chat", "embedding"], "group": "admin-team",
    }]
    store = SQLiteStore(str(tmp_path / "scrub-auth.db"))
    async def inline_db(fn):
        return fn()
    store._db = inline_db
    await store.seed_from_config(config)

    # Simulate the installer-written .env with the seeded ADMIN_API_KEY.
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_API_KEY=sk-keep-me\n"
        "ADMIN_API_KEY=gw-default-admin-key-1234567890\n"
        "OTHER_VAR=untouched\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    app = FastAPI()
    app.state.key_store = store
    app.include_router(auth_router, prefix="/auth")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as client:
        await client.post("/auth/session", json={"api_key": old_key})
        reset = await client.post(
            "/auth/reset-password", json={"new_api_key": new_key},
        )
        assert reset.status_code == 200

    # The ADMIN_API_KEY line must be gone, other lines preserved.
    remaining = env_file.read_text(encoding="utf-8")
    assert "ADMIN_API_KEY=" not in remaining
    assert "sk-keep-me" in remaining
    assert "untouched" in remaining
    # The new key must NOT have been written back to .env.
    assert new_key not in remaining


@pytest.mark.asyncio
async def test_check_is_default_false_after_revocation(tmp_path: Path):
    """A revoked default key must not count as 'still default'.

    Regression guard for the bootstrap prefill endpoint: after a force-reset
    the old key is revoked but kept is_default=1. check_is_default must return
    False so bootstrap stops offering the dead credential.
    """
    old_key = "gw-default-admin-key-1234567890"
    new_key = "gw-replacement-admin-key-0987654321"
    config = [{
        "key": old_key, "user_id": "admin",
        "scopes": ["admin", "chat", "embedding"], "group": "admin-team",
    }]
    store = SQLiteStore(str(tmp_path / "revoked-auth.db"))
    async def inline_db(fn):
        return fn()
    store._db = inline_db
    await store.seed_from_config(config)

    old_hash = _hash_key(old_key)
    assert await store.check_is_default(old_hash) is True

    # Force-reset: revoke old, create new (mirrors reset_password internals).
    now_iso = datetime.now(timezone.utc).isoformat()
    with store.conn.transaction() as tx:
        tx.execute(
            "UPDATE api_keys SET status='revoked', rotated_at=?, revoked_at=? WHERE key_hash=?",
            (now_iso, now_iso, old_hash),
        )
        tx.execute(
            "INSERT INTO api_keys (key_hash, key_id, key_prefix, user_id, status, "
            "is_default, created_at, last_used_at, expires_at, scopes, group_id, "
            "cache_scope, daily_tokens_limit, daily_tokens_used, monthly_cost_limit, "
            "monthly_cost_used, rate_limit_rpm, rate_limit_tpm, rpm_window_start, "
            "rpm_window_count, tpm_window_start, tpm_window_count, is_admin) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_hash_key(new_key), "key_new1", new_key[:8], "admin", "active", 0,
             now_iso, "", None, "admin,chat,embedding", "admin-team", "group",
             1000000, 0, 50.0, 0.0, 60, 100000, 0, 0, 0, 0, 1),
        )

    # Revoked old key no longer counts as default.
    assert await store.check_is_default(old_hash) is False
    # The new key was never default (is_default=0).
    assert await store.check_is_default(_hash_key(new_key)) is False


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
