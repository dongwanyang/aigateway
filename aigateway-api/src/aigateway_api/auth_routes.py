"""Browser account and session endpoints for the control panel."""
from __future__ import annotations

import os
import secrets
import tempfile
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from .auth_middleware import SESSION_COOKIE_NAME, _hash_key
from .browser_auth import get_browser_auth_store

router = APIRouter()


_INITIAL_PASSWORD_ENV_NAMES = (
    "AI_GATEWAY_INITIAL_ADMIN_PASSWORD",
    "AI_GATEWAY_ADMIN_PASSWORD",
    "ADMIN_PASSWORD",
)


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


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


def _initial_admin_password() -> str:
    """Return the installer-generated temporary console password, if present."""
    for name in _INITIAL_PASSWORD_ENV_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _initial_admin_key(request: Request) -> str:
    """Find the legacy installer admin API key for one-time migrations.

    New installations should use AI_GATEWAY_INITIAL_ADMIN_PASSWORD instead. This
    fallback preserves the old auto-prefill/first-login behavior for existing
    installations whose installer only produced ADMIN_API_KEY or config.yaml
    auth.api_keys.
    """
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
            if isinstance(scopes, str):
                scopes = [item.strip() for item in scopes.split(",")]
            if (
                entry.get("user_id") == "admin"
                and "admin" in scopes
                and isinstance(entry.get("key"), str)
            ):
                return entry["key"].strip()
    except Exception:
        return ""
    return ""


def _scrub_initial_password_from_env() -> None:
    """Best-effort removal of one-time console passwords from .env.

    This mirrors the old force-reset safety property: after the operator has set
    the real admin password, a future DB wipe should not silently resurrect the
    installer temporary password from plaintext config. ADMIN_API_KEY is not
    removed here because API keys are now separate machine credentials.
    """
    env_path = os.path.join(os.getcwd(), ".env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return

    prefixes = tuple(f"{name}=" for name in _INITIAL_PASSWORD_ENV_NAMES)
    kept = [line for line in lines if not line.strip().startswith(prefixes)]
    if len(kept) == len(lines):
        return

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


class CreateSessionRequest(BaseModel):
    username: str | None = Field(default=None, min_length=1)
    password: str | None = Field(default=None, min_length=1)
    # Deprecated compatibility field. It is disabled by default so raw API keys
    # are not accepted as console sessions unless an operator explicitly opts in.
    api_key: str | None = Field(default=None, min_length=1)


class ResetPasswordRequest(BaseModel):
    new_password: str | None = Field(default=None, min_length=12)
    # Deprecated JSON field kept so an already-open old control panel can still
    # complete first-login migration after the backend is upgraded.
    new_api_key: str | None = Field(default=None, min_length=12)

    def password_value(self) -> str:
        return (self.new_password or self.new_api_key or "").strip()


async def _legacy_admin_key(request: Request, candidate: str, *, require_default: bool) -> bool:
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
    if not key_data or "admin" not in scopes:
        return False
    if require_default:
        try:
            check = getattr(key_store, "check_is_default", None)
            return bool(check and await check(_hash_key(candidate)))
        except Exception:
            return False
    return True


async def _legacy_default_key_available(request: Request, candidate: str) -> bool:
    """Read-only check used by /bootstrap before exposing legacy credentials."""
    key_store = getattr(request.app.state, "key_store", None)
    check = getattr(key_store, "check_is_default", None) if key_store is not None else None
    if check is None:
        return False
    try:
        return bool(await check(_hash_key(candidate)))
    except Exception:
        return False


async def _is_valid_initial_secret(request: Request, candidate: str) -> bool:
    configured_password = _initial_admin_password()
    if configured_password:
        try:
            return secrets.compare_digest(
                candidate.encode("utf-8"), configured_password.encode("utf-8")
            )
        except Exception:
            return False
    # Existing deployments did not have an admin-password env var. For the
    # first login only, accept the still-active default admin API key as a
    # temporary console password and force a password change immediately.
    legacy_key = _initial_admin_key(request)
    if not legacy_key:
        return False
    try:
        if not secrets.compare_digest(candidate.encode("utf-8"), legacy_key.encode("utf-8")):
            return False
    except Exception:
        return False
    return await _legacy_admin_key(request, candidate, require_default=True)


@router.post("/session")
async def create_session(
    request: Request, response: Response, body: CreateSessionRequest
) -> Dict[str, Any]:
    username = (body.username or os.environ.get("AI_GATEWAY_ADMIN_USERNAME", "admin")).strip()
    password = body.password

    if body.api_key:
        if not _truthy(os.environ.get("AI_GATEWAY_ALLOW_API_KEY_CONSOLE_LOGIN")):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": {
                        "code": "api_key_login_disabled",
                        "message": "API Key login is disabled. Use the administrator account.",
                    }
                },
            )
        # Explicit emergency compatibility: treat api_key as a temporary password
        # only for first-login migration, never as a cookie value.
        password = body.api_key

    if not password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"code": "validation_error", "message": "Provide username and password"}},
        )

    store = get_browser_auth_store(request)
    user = store.verify_credentials(username, password)

    if user is None and not store.has_users():
        expected_username = os.environ.get("AI_GATEWAY_ADMIN_USERNAME", "admin")
        try:
            username_ok = secrets.compare_digest(
                username.encode("utf-8"), expected_username.encode("utf-8")
            )
        except Exception:
            username_ok = False
        if username_ok and await _is_valid_initial_secret(request, password):
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
    """Expose installer credentials only before the admin account is provisioned."""
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    store = get_browser_auth_store(request)
    if not _truthy(os.environ.get("AI_GATEWAY_PREFILL_INITIAL_CREDENTIALS")) or store.has_users():
        return {"data": {"available": False}, "message": "success"}

    initial_password = _initial_admin_password()
    if initial_password:
        return {
            "data": {
                "available": True,
                "username": os.environ.get("AI_GATEWAY_ADMIN_USERNAME", "admin"),
                "initial_password": initial_password,
            },
            "message": "success",
        }

    legacy_key = _initial_admin_key(request)
    if not legacy_key or not await _legacy_default_key_available(request, legacy_key):
        return {"data": {"available": False}, "message": "success"}
    return {
        "data": {
            "available": True,
            "username": os.environ.get("AI_GATEWAY_ADMIN_USERNAME", "admin"),
            "initial_password": legacy_key,
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
    new_password = body.password_value()
    if not new_password:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "validation_error", "message": "Provide a new administrator password"}},
        )

    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        raise HTTPException(status_code=401, detail={"error": {"code": "unauthorized", "message": "Authentication required"}})
    store = get_browser_auth_store(request)
    session = store.validate_session(raw_token, idle_ttl_seconds=_session_ttl())
    if session is None:
        raise HTTPException(status_code=401, detail={"error": {"code": "unauthorized", "message": "Invalid session"}})

    store.change_password(str(session["user_id"]), new_password)
    _scrub_initial_password_from_env()
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
            "password_changed": True,
            "warning": "Administrator password updated. Existing browser sessions were revoked.",
        },
        "message": "Password reset successful",
    }
