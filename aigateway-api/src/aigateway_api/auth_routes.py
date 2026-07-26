"""Browser account and session endpoints for the control panel."""
from __future__ import annotations

import os
import secrets
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from .auth_middleware import SESSION_COOKIE_NAME
from .browser_auth import get_browser_auth_store

router = APIRouter()


def _is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or forwarded.split(",", 1)[0].strip() == "https"


def _session_ttl() -> int:
    return int(os.environ.get("AI_GATEWAY_SESSION_TTL_SECONDS", "28800"))


def _absolute_session_ttl() -> int:
    return int(os.environ.get("AI_GATEWAY_SESSION_ABSOLUTE_TTL_SECONDS", "86400"))


def _set_session_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=_session_ttl(),
        httponly=True,
        secure=_is_https(request),
        samesite="strict",
        path="/",
    )


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else ""


class CreateSessionRequest(BaseModel):
    username: str | None = Field(default=None, min_length=1)
    password: str | None = Field(default=None, min_length=1)
    api_key: str | None = Field(default=None, min_length=1)


class ResetPasswordRequest(BaseModel):
    # Keep the old JSON field temporarily so existing control-panel releases can
    # complete the first-login migration without a lockout.
    new_api_key: str = Field(..., min_length=12, description="New administrator password")


async def _legacy_admin_key(request: Request, candidate: str) -> bool:
    key_store = getattr(request.app.state, "key_store", None)
    if key_store is None:
        return False
    try:
        key_data = await key_store.validate(candidate)
    except Exception:
        return False
    scopes = key_data.get("scopes", []) if key_data else []
    if isinstance(scopes, str):
        scopes = [item.strip() for item in scopes.split(",")]
    return bool(key_data and "admin" in scopes)


@router.post("/session")
async def create_session(
    request: Request, response: Response, body: CreateSessionRequest
) -> Dict[str, Any]:
    if body.api_key and not (
        os.environ.get("AI_GATEWAY_ALLOW_API_KEY_CONSOLE_LOGIN", "false").strip().lower()
        in {"1", "true", "yes", "on"}
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "api_key_login_disabled",
                    "message": "API Key login is disabled. Use the administrator account.",
                }
            },
        )

    username = (body.username or os.environ.get("AI_GATEWAY_ADMIN_USERNAME", "admin")).strip()
    password = body.password or body.api_key
    if not password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"code": "validation_error", "message": "Provide username and password"}},
        )

    store = get_browser_auth_store(request)
    user = store.verify_credentials(username, password)

    # Backward-compatible one-time migration: when no admin account exists, the
    # existing admin API key may bootstrap the account. It is never stored in the
    # cookie; the resulting session is opaque and password change is mandatory.
    if user is None and not store.has_users():
        expected_username = os.environ.get("AI_GATEWAY_ADMIN_USERNAME", "admin")
        try:
            username_ok = secrets.compare_digest(
                username.encode("utf-8"), expected_username.encode("utf-8")
            )
        except Exception:
            username_ok = False
        if username_ok and await _legacy_admin_key(request, password):
            user = store.provision_admin(username, password)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"code": "unauthorized", "message": "Invalid username or password"}},
        )

    token = store.create_session(
        str(user["user_id"]),
        ttl_seconds=_session_ttl(),
        absolute_ttl_seconds=_absolute_session_ttl(),
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    _set_session_cookie(request, response, token)

    return {
        "data": {
            "authenticated": True,
            "key_prefix": username,
            "scopes": ["admin", "chat", "embedding"],
            "force_reset": bool(user.get("requires_password_change")),
        },
        "message": "success",
    }


@router.get("/bootstrap")
async def get_bootstrap_credentials(request: Request, response: Response) -> Dict[str, Any]:
    """Expose legacy installer credentials only until the admin account is provisioned."""
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    store = get_browser_auth_store(request)
    enabled = (
        os.environ.get("AI_GATEWAY_PREFILL_INITIAL_CREDENTIALS", "false").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    initial = os.environ.get("ADMIN_API_KEY", "").strip()
    if not enabled or store.has_users() or not initial:
        return {"data": {"available": False}, "message": "success"}
    return {
        "data": {
            "available": True,
            "username": os.environ.get("AI_GATEWAY_ADMIN_USERNAME", "admin"),
            "initial_password": initial,
        },
        "message": "success",
    }


@router.get("/session")
async def get_session(request: Request) -> Dict[str, Any]:
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        return {"data": {"authenticated": False}}
    store = get_browser_auth_store(request)
    session = store.validate_session(raw_token, idle_ttl_seconds=_session_ttl())
    if session is None:
        return {"data": {"authenticated": False}}
    return {
        "data": {
            "authenticated": True,
            "key_prefix": session.get("username", "admin"),
            "scopes": ["admin", "chat", "embedding"],
            "force_reset": bool(session.get("requires_password_change")),
        },
        "message": "success",
    }


@router.delete("/session")
async def delete_session(request: Request, response: Response) -> Dict[str, Any]:
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_token:
        get_browser_auth_store(request).revoke_session(raw_token)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", samesite="strict")
    return {"data": {"authenticated": False}, "message": "success"}


@router.post("/reset-password")
async def reset_password(
    request: Request,
    response: Response,
    body: ResetPasswordRequest,
) -> Dict[str, Any]:
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        raise HTTPException(status_code=401, detail={"error": {"code": "unauthorized", "message": "Authentication required"}})
    store = get_browser_auth_store(request)
    session = store.validate_session(raw_token, idle_ttl_seconds=_session_ttl())
    if session is None:
        raise HTTPException(status_code=401, detail={"error": {"code": "unauthorized", "message": "Invalid session"}})

    new_password = body.new_api_key
    store.change_password(str(session["user_id"]), new_password)
    new_token = store.create_session(
        str(session["user_id"]),
        ttl_seconds=_session_ttl(),
        absolute_ttl_seconds=_absolute_session_ttl(),
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    _set_session_cookie(request, response, new_token)
    return {
        "data": {
            "new_api_key": new_password,
            "warning": "Administrator password updated. Existing browser sessions were revoked.",
        },
        "message": "Password reset successful",
    }
