"""Browser session endpoints.

The control panel exchanges an API key once for an HttpOnly, SameSite cookie.
JavaScript never persists or reads the secret after login.
"""
from __future__ import annotations

import os
import secrets
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
    api_key: str = Field(..., min_length=1)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_unix() -> int:
    return int(datetime.now(timezone.utc).timestamp())


@router.post("/session")
async def create_session(
    request: Request, response: Response, body: CreateSessionRequest
) -> Dict[str, Any]:
    key_store = getattr(request.app.state, "key_store", None)
    if key_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "unavailable", "message": "Authentication service unavailable"}},
        )
    try:
        key_data = await key_store.validate(body.api_key)
    except Exception:
        key_data = None
    if key_data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"code": "unauthorized", "message": "Invalid API key"}},
        )

    max_age = int(os.environ.get("AI_GATEWAY_SESSION_TTL_SECONDS", "28800"))
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=body.api_key,
        max_age=max_age,
        httponly=True,
        secure=_is_https(request),
        samesite="strict",
        path="/",
    )

    # Check if this is the default admin key (needs force-reset on first login)
    key_hash = _hash_key(body.api_key)
    is_default = False
    if hasattr(key_store, "check_is_default"):
        is_default = await key_store.check_is_default(key_hash)

    return {
        "data": {
            "authenticated": True,
            "key_prefix": key_data.get("key_prefix", body.api_key[:8]),
            "scopes": key_data.get("scopes", []),
            "force_reset": is_default,
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
