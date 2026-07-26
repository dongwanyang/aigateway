"""Browser session endpoints.

The control panel exchanges an API key once for an HttpOnly, SameSite cookie.
JavaScript never persists or reads the secret after login.
"""
from __future__ import annotations

import os
import secrets
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from .auth_middleware import SESSION_COOKIE_NAME, _hash_key, authenticate

router = APIRouter()


def _is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or forwarded.split(",", 1)[0].strip() == "https"


class CreateSessionRequest(BaseModel):
    api_key: str | None = Field(default=None, min_length=1)
    username: str | None = Field(default=None, min_length=1)
    password: str | None = Field(default=None, min_length=1)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_unix() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _initial_admin_key(request: Request) -> str:
    """Find the installer key in env, with legacy config.yaml compatibility."""
    env_key = os.environ.get("ADMIN_API_KEY", "").strip()
    if env_key:
        return env_key

    config_manager = getattr(request.app.state, "config_manager", None)
    if config_manager is None:
        return ""
    try:
        auth_config = config_manager.get("auth", {}) or {}
        for entry in auth_config.get("api_keys", []) or []:
            if not isinstance(entry, dict):
                continue
            scopes = entry.get("scopes", [])
            if (
                entry.get("user_id") == "admin"
                and "admin" in scopes
                and isinstance(entry.get("key"), str)
            ):
                return entry["key"].strip()
    except Exception:
        return ""
    return ""


def _scrub_admin_key_from_env() -> None:
    """Remove the ``ADMIN_API_KEY=`` line from the on-disk ``.env``.

    Called after a successful force-reset. The installer-seeded key in ``.env``
    is a *first-boot seed* only — once the operator has rotated it, leaving the
    old key on disk means a future DB wipe / re-install would reseed the
    *revoked* key back to ``is_default=1, active``, silently resurrecting a
    credential the operator believed dead. Deleting the line (rather than
    writing the new key back) keeps runtime secrets out of the plaintext
    config and cuts the resurrection path: ``${ADMIN_API_KEY:-}`` resolves to
    empty, and ``seed_from_config`` skips empty keys.

    Atomic (tempfile + ``os.replace``) so a concurrent ``load_dotenv`` reader
    never sees a half-written file. Best-effort: missing file or permission
    errors are swallowed (the reset itself already succeeded in SQLite).
    """
    env_path = os.path.join(os.getcwd(), ".env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return

    kept = [ln for ln in lines if not ln.strip().startswith("ADMIN_API_KEY=")]
    if len(kept) == len(lines):
        return  # Nothing to remove (env var may have come from the real env).

    try:
        target_dir = os.path.dirname(env_path) or "."
        fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".env.tmp.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.writelines(kept)
            os.replace(tmp_path, env_path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
    except OSError:
        return


@router.post("/session")
async def create_session(
    request: Request, response: Response, body: CreateSessionRequest
) -> Dict[str, Any]:
    account_login = False
    if body.api_key:
        login_key = body.api_key
    elif body.username is not None and body.password is not None:
        account_login = True
        login_key = body.password
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "validation_error",
                    "message": "Provide an API key or username and password",
                }
            },
        )

    key_store = getattr(request.app.state, "key_store", None)
    if key_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "unavailable", "message": "Authentication service unavailable"}},
        )
    try:
        key_data = await key_store.validate(login_key)
    except Exception:
        key_data = None
    if account_login and key_data is not None:
        # Constant-time username check (bytes to tolerate non-ASCII input;
        # secrets.compare_digest raises TypeError on non-ASCII str). Done after
        # validate() so the wrong-username and wrong-password paths both pay the
        # key-store lookup cost and don't leak username validity via timing.
        expected_username = os.environ.get("AI_GATEWAY_ADMIN_USERNAME", "admin")
        username_match = False
        try:
            username_match = secrets.compare_digest(
                body.username.encode("utf-8"),
                expected_username.encode("utf-8"),
            )
        except Exception:
            username_match = False
        if not username_match:
            key_data = None
        else:
            scopes = key_data.get("scopes", [])
            if "admin" not in scopes:
                key_data = None
    if key_data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "unauthorized",
                    "message": (
                        "Invalid username or password"
                        if account_login
                        else "Invalid API key"
                    ),
                }
            },
        )

    max_age = int(os.environ.get("AI_GATEWAY_SESSION_TTL_SECONDS", "28800"))
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=login_key,
        max_age=max_age,
        httponly=True,
        secure=_is_https(request),
        samesite="strict",
        path="/",
    )

    # Check if this is the default admin key (needs force-reset on first login)
    key_hash = _hash_key(login_key)
    is_default = False
    if hasattr(key_store, "check_is_default"):
        is_default = await key_store.check_is_default(key_hash)

    return {
        "data": {
            "authenticated": True,
            "key_prefix": key_data.get("key_prefix", login_key[:8]),
            "scopes": key_data.get("scopes", []),
            "force_reset": is_default,
        },
        "message": "success",
    }


@router.get("/bootstrap")
async def get_bootstrap_credentials(request: Request, response: Response) -> Dict[str, Any]:
    """Return one-time installer credentials while the generated key is still default.

    Off by default; installers opt in with
    AI_GATEWAY_PREFILL_INITIAL_CREDENTIALS=true for freshly-installed local
    instances. The endpoint stops exposing credentials automatically as soon as
    the default key is rotated/revoked.
    """
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"

    # Default off in all environments — credential prefill is a convenience for
    # a freshly-installed local instance only. Installers opt in by setting
    # AI_GATEWAY_PREFILL_INITIAL_CREDENTIALS=true in the .env they generate; any
    # other deployment (especially internet-facing) stays safe by default.
    enabled = (
        os.environ.get(
            "AI_GATEWAY_PREFILL_INITIAL_CREDENTIALS", "false"
        ).strip().lower()
        not in {"0", "false", "no", "off"}
    )
    initial_key = _initial_admin_key(request)
    key_store = getattr(request.app.state, "key_store", None)
    if not enabled or not initial_key or key_store is None:
        return {"data": {"available": False}, "message": "success"}

    # Read-only check only: avoid key_store.validate(), which writes
    # last_used_at on every call and would let bootstrap polling pollute the
    # admin key's audit trail / first-login timestamp.
    try:
        is_default = (
            await key_store.check_is_default(_hash_key(initial_key))
            if hasattr(key_store, "check_is_default")
            else False
        )
    except Exception:
        is_default = False

    if not is_default:
        return {"data": {"available": False}, "message": "success"}

    return {
        "data": {
            "available": True,
            "username": os.environ.get("AI_GATEWAY_ADMIN_USERNAME", "admin"),
            "initial_password": initial_key,
        },
        "message": "success",
    }


@router.get("/session")
async def get_session(request: Request) -> Dict[str, Any]:
    """Check if the current session cookie is valid. Returns 200 with key info or 401."""
    raw_key = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_key:
        return {"data": {"authenticated": False}}

    key_store = getattr(request.app.state, "key_store", None)
    if key_store is None:
        return {"data": {"authenticated": False}}

    try:
        key_data = await key_store.validate(raw_key)
    except Exception:
        key_data = None

    if key_data is None:
        return {"data": {"authenticated": False}}

    key_hash = _hash_key(raw_key)
    is_default = False
    if hasattr(key_store, "check_is_default"):
        is_default = await key_store.check_is_default(key_hash)

    return {
        "data": {
            "authenticated": True,
            "key_prefix": key_data.get("key_prefix", raw_key[:8]),
            "scopes": key_data.get("scopes", []),
            "force_reset": is_default,
        },
        "message": "success",
    }


@router.delete("/session")
async def delete_session(response: Response) -> Dict[str, Any]:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", samesite="strict")
    return {"data": {"authenticated": False}, "message": "success"}


class ResetPasswordRequest(BaseModel):
    new_api_key: str = Field(..., min_length=20, description="New API key (min 20 characters)")


@router.post("/reset-password")
async def reset_password(
    request: Request,
    response: Response,
    body: ResetPasswordRequest,
    _auth: Dict[str, Any] = Depends(authenticate),
) -> Dict[str, Any]:
    """Reset the current session's API Key (only for default admin).

    Atomically revokes the old key and issues a replacement. The new key is
    returned once in the response body and also set as the session cookie.
    """
    key_store = getattr(request.app.state, "key_store", None)
    if key_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "unavailable", "message": "Authentication service unavailable"}},
        )

    # Use the authenticated key hash from middleware. The lookup and
    # replacement run in one SQLite transaction below.
    current_hash = _hash_key(request.state.api_key_value)
    new_key = body.new_api_key
    new_hash = _hash_key(new_key)
    if new_hash == current_hash:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "validation_error", "message": "New API key must differ from the current key"}},
        )

    now_iso = _now_iso()

    def _replace_default_key() -> None:
        # Atomic: revoke old + create new + quota records + group membership.
        with key_store.conn.transaction() as tx:
            row = tx.execute(
                "SELECT * FROM api_keys WHERE key_hash=?", (current_hash,)
            ).fetchone()
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error": {"code": "not_found", "message": "Key not found"}},
                )
            key_info = dict(row)
            if not key_info.get("is_default"):
                raise HTTPException(
                    status_code=403,
                    detail={"error": {"code": "forbidden", "message": "Only the default admin key can be reset"}},
                )

            tx.execute(
                """UPDATE api_keys
                   SET status='revoked', rotated_at=?, revoked_at=?
                   WHERE key_hash=?""",
                (now_iso, now_iso, current_hash),
            )

            scopes = key_info.get("scopes", "chat,embedding")
            if isinstance(scopes, list):
                scopes = ",".join(scopes)
            group_id = key_info.get("group_id", "") or ""
            cache_scope = key_info.get("cache_scope", "group") or "group"
            daily_limit = key_info.get("daily_tokens_limit", 1_000_000)
            monthly_cost_limit = key_info.get("monthly_cost_limit", 50.0)
            rate_rpm = key_info.get("rate_limit_rpm", 60)
            rate_tpm = key_info.get("rate_limit_tpm", 100_000)
            is_admin = int(key_info.get("is_admin", 0))
            expires_at = key_info.get("expires_at")

            tx.execute(
                """INSERT INTO api_keys
                   (key_hash, key_id, key_prefix, user_id, status, is_default,
                    created_at, last_used_at, expires_at, scopes,
                    group_id, cache_scope,
                    daily_tokens_limit, daily_tokens_used,
                    monthly_cost_limit, monthly_cost_used,
                    rate_limit_rpm, rate_limit_tpm,
                    rpm_window_start, rpm_window_count,
                    tpm_window_start, tpm_window_count, is_admin)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_hash, f"key_{secrets.token_hex(8)[:8]}", new_key[:8],
                    key_info["user_id"], "active", 0, now_iso, "",
                    expires_at, scopes,
                    group_id, cache_scope,
                    daily_limit, 0,
                    monthly_cost_limit, 0.0,
                    rate_rpm, rate_tpm,
                    _now_unix(), 0, _now_unix(), 0,
                    is_admin,
                ),
            )

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            month = datetime.now(timezone.utc).strftime("%Y-%m")
            qb = {
                "tokens_in": 0, "tokens_out": 0,
                "cost_usd": 0.0, "request_count": 0,
                "model_usage": "{}",
            }
            tx.execute(
                """INSERT INTO quota_records
                   (entity_type, entity_id, period_type, period_value,
                    tokens_in, tokens_out, cost_usd, request_count, model_usage)
                   VALUES ('key', ?, 'daily', ?, ?, ?, ?, ?, ?)""",
                (new_hash, today, qb["tokens_in"], qb["tokens_out"],
                 qb["cost_usd"], qb["request_count"], qb["model_usage"]),
            )
            tx.execute(
                """INSERT INTO quota_records
                   (entity_type, entity_id, period_type, period_value,
                    tokens_in, tokens_out, cost_usd, request_count, model_usage)
                   VALUES ('key', ?, 'monthly', ?, ?, ?, ?, ?, ?)""",
                (new_hash, month, qb["tokens_in"], qb["tokens_out"],
                 qb["cost_usd"], qb["request_count"], qb["model_usage"]),
            )

            if group_id:
                tx.execute(
                    "UPDATE group_members SET key_hash=? WHERE key_hash=?",
                    (new_hash, current_hash),
                )

    try:
        await key_store._db(_replace_default_key)
    except HTTPException:
        raise
    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            raise HTTPException(
                status_code=409,
                detail={"error": {"code": "conflict", "message": "API key already exists"}},
            ) from exc
        raise

    # The installer-seeded ADMIN_API_KEY in .env is now stale (points at the
    # revoked key). Scrub it so a future DB wipe can't resurrect the old
    # credential. See _scrub_admin_key_from_env for the threat model.
    _scrub_admin_key_from_env()

    # Set new session cookie
    max_age = int(os.environ.get("AI_GATEWAY_SESSION_TTL_SECONDS", "28800"))
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=new_key,
        max_age=max_age, httponly=True, secure=_is_https(request),
        samesite="strict", path="/",
    )

    return {
        "data": {
            "new_api_key": new_key,
            "warning": "This key is shown only once — save it immediately!",
        },
        "message": "Password reset successful",
    }
