"""Authentication dependencies for API keys and browser sessions."""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Dict, Optional

from fastapi import Header, HTTPException, Request, status

from .browser_auth import get_browser_auth_store

logger = logging.getLogger(__name__)
SESSION_COOKIE_NAME = "aigateway_session"


def _extract_api_key(
    authorization: Optional[str] = None,
    x_api_key: Optional[str] = None,
) -> Optional[str]:
    if x_api_key:
        return x_api_key
    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1]
    return None


def _hash_key(key_value: str) -> str:
    """Return the canonical API-key hash used by SQLiteStore and legacy tests."""
    return hashlib.sha256(key_value.encode("utf-8")).hexdigest()


def _get_session_cookie(request: Request) -> Optional[str]:
    value = request.cookies.get(SESSION_COOKIE_NAME)
    return value if isinstance(value, str) and value else None


def require_scope(key_data: Dict[str, Any], scope: str) -> None:
    scopes = key_data.get("scopes") or []
    if isinstance(scopes, str):
        scopes = [item.strip() for item in scopes.split(",")]
    if scope not in scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": {
                    "code": "insufficient_scope",
                    "message": f"Credential requires '{scope}' scope",
                }
            },
        )


def _api_key_required(headers: Optional[Dict[str, str]] = None) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": {"code": "unauthorized", "message": "API key required"}},
        headers=headers,
    )


def _password_change_required() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error": {
                "code": "password_change_required",
                "message": "Administrator password change is required before using this endpoint.",
            }
        },
    )


def _reject_force_reset(principal: Dict[str, Any]) -> None:
    if principal.get("auth_type") == "browser_session" and principal.get("requires_password_change"):
        raise _password_change_required()


async def _authenticate_api_key(request: Request, key_value: str) -> Dict[str, Any]:
    key_store = getattr(request.app.state, "key_store", None)
    if key_store is None:
        logger.error("KeyStore is not initialized")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": {"code": "internal_error", "message": "Authentication service unavailable"}},
        )
    try:
        key_data = await key_store.validate(key_value)
    except Exception as exc:
        message = str(exc).lower()
        if "revoked" in message:
            code, http_status, text = "forbidden", 403, "API key has been revoked"
        elif "suspended" in message:
            code, http_status, text = "forbidden", 403, "API key is suspended"
        elif "expired" in message or "expiration" in message:
            code, http_status, text = "key_expired", 403, "API key has expired"
        else:
            code, http_status, text = "unauthorized", 401, "Invalid or missing API key"
        raise HTTPException(
            status_code=http_status,
            detail={"error": {"code": code, "message": text}},
        ) from exc
    if key_data is None:
        raise _api_key_required()
    request.state.auth_type = "api_key"
    request.state.api_key_data = key_data
    request.state.api_key_value = key_value
    request.state.api_key_hash = _hash_key(key_value)
    return key_data


async def _authenticate_browser_session(request: Request, token: str) -> Dict[str, Any]:
    import os

    ttl = int(os.environ.get("AI_GATEWAY_SESSION_TTL_SECONDS", "28800"))
    store = get_browser_auth_store(request)
    session = await asyncio.to_thread(store.validate_session, token, idle_ttl_seconds=ttl)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"code": "unauthorized", "message": "Invalid or expired browser session"}},
        )
    principal = {
        "key_id": f"session:{session['user_id']}",
        "user_id": session["user_id"],
        "username": session.get("username", "admin"),
        "status": "active",
        "scopes": ["admin", "chat", "embedding"],
        "auth_type": "browser_session",
        "requires_password_change": bool(session.get("requires_password_change")),
    }
    request.state.auth_type = "browser_session"
    request.state.browser_session = session
    request.state.api_key_data = principal
    request.state.api_key_value = None
    return principal


async def authenticate_api_key(
    request: Request,
    api_key: Optional[str] = Header(None, alias="x-api-key"),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Authenticate machine/API endpoints with API-key headers only."""
    key_value = _extract_api_key(authorization, api_key)
    if not key_value:
        raise _api_key_required(headers={"WWW-Authenticate": "Bearer"})
    return await _authenticate_api_key(request, key_value)


async def authenticate(
    request: Request,
    api_key: Optional[str] = Header(None, alias="x-api-key"),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Compatibility dependency for /v1 endpoints: API-key headers only."""
    return await authenticate_api_key(request, api_key=api_key, authorization=authorization)


async def authenticate_admin(request: Request) -> Dict[str, Any]:
    """Authenticate administrator routes with admin API keys or browser sessions.

    Browser sessions that still require an administrator password change may only
    call auth/session endpoints directly; admin routes fail closed server-side.
    """
    key_value = _extract_api_key(
        request.headers.get("authorization"),
        request.headers.get("x-api-key"),
    )
    if key_value:
        principal = await _authenticate_api_key(request, key_value)
    else:
        token = _get_session_cookie(request)
        if not token:
            raise _api_key_required(headers={"WWW-Authenticate": "Bearer"})
        principal = await _authenticate_browser_session(request, token)
    require_scope(principal, "admin")
    _reject_force_reset(principal)
    return principal
