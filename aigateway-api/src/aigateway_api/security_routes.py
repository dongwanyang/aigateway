"""Security-first replacements for sensitive legacy admin endpoints."""
from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .auth_middleware import authenticate_admin
from .config_security import (
    ConfigPreconditionRequiredError,
    ConfigUpdateBusyError,
    ConfigValidationError,
    ConfigVersionConflictError,
    config_revision,
    read_versioned_yaml_config,
    redact_config,
    transactional_replace_config,
)
from .safe_http import fetch_public_text, validate_public_url

router = APIRouter()


def _manager(request: Request) -> Any:
    manager = getattr(request.app.state, "config_manager", None)
    if manager is None:
        raise HTTPException(
            500,
            detail={
                "error": {
                    "code": "internal_error",
                    "message": "ConfigManager not initialized",
                }
            },
        )
    return manager


def _config_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ConfigPreconditionRequiredError):
        return HTTPException(
            428,
            detail={
                "error": {
                    "code": "config_revision_required",
                    "message": str(exc),
                }
            },
        )
    if isinstance(exc, ConfigVersionConflictError):
        return HTTPException(
            409,
            detail={
                "error": {
                    "code": "config_version_conflict",
                    "message": str(exc),
                    "expected_revision": exc.expected,
                    "current_revision": exc.current,
                }
            },
        )
    if isinstance(exc, ConfigUpdateBusyError):
        return HTTPException(
            409,
            detail={
                "error": {
                    "code": "config_update_busy",
                    "message": str(exc),
                }
            },
        )
    if isinstance(exc, ConfigValidationError):
        messages = [
            str(issue.get("message", "invalid configuration"))
            for issue in exc.issues
        ]
        return HTTPException(
            422,
            detail={
                "error": {
                    "code": "validation_error",
                    "message": "; ".join(messages),
                    "issues": exc.issues,
                }
            },
        )
    return HTTPException(
        500,
        detail={
            "error": {
                "code": "config_update_failed",
                "message": "Configuration update failed",
            }
        },
    )


def _expected_revision(request: Request) -> str:
    # Browsers may use the query parameter because the existing deployment CORS
    # contract does not permit the non-safelisted If-Match request header. Other
    # clients should prefer the standard strong If-Match precondition.
    raw = (
        request.headers.get("if-match", "").strip()
        or request.query_params.get("revision", "").strip()
    )
    if not raw:
        raise ConfigPreconditionRequiredError(
            "If-Match or revision query precondition is required"
        )
    if raw.startswith("W/") or "," in raw or raw == "*":
        raise ConfigPreconditionRequiredError(
            "revision must contain one strong configuration revision"
        )
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        raw = raw[1:-1]
    if not raw:
        raise ConfigPreconditionRequiredError(
            "configuration revision is empty"
        )
    return raw


def _commit_revision(commit: Any, path: str) -> str:
    revision = getattr(commit, "revision", None)
    if isinstance(revision, str) and revision:
        return revision
    return config_revision(path)


def _versioned_response(data: dict[str, Any], revision: str) -> JSONResponse:
    return JSONResponse(
        content={"data": data, "message": "success", "revision": revision},
        headers={"ETag": f'"{revision}"'},
    )


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(
            400,
            detail={
                "error": {
                    "code": "validation_error",
                    "message": "Invalid JSON body",
                }
            },
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(
            400,
            detail={
                "error": {
                    "code": "validation_error",
                    "message": "Request body must be an object",
                }
            },
        )
    return body


@router.get("/config")
async def get_secure_full_config(
    request: Request,
    _auth: dict[str, Any] = Depends(authenticate_admin),
):
    manager = _manager(request)
    loaded, revision = read_versioned_yaml_config(manager.config_path)
    return _versioned_response(redact_config(loaded), revision)


@router.put("/config")
async def update_secure_full_config(
    request: Request,
    _auth: dict[str, Any] = Depends(authenticate_admin),
):
    manager = _manager(request)
    try:
        expected = _expected_revision(request)
        submitted = await _json_object(request)
        writable = {
            "server",
            "plugins",
            "providers",
            "embedding",
            "observability",
            "infrastructure",
            "cache",
            "circuit_breaker",
            "rate_limiter",
            "streaming",
            "generation_optimization",
            "code_rag",
            "plugin_runtime",
            "retry_budget",
            "intent_classifier",
            "model_selector",
            "task_routing",
            "generation",
            "media_optimization",
            "hot_reload",
            "debug_mode",
            "debug",
        }
        candidate, _revision = read_versioned_yaml_config(manager.config_path)
        for key in writable:
            if key in submitted:
                candidate[key] = submitted[key]
        commit = transactional_replace_config(
            manager.config_path,
            candidate,
            manager,
            expected_revision=expected,
        )
    except Exception as exc:
        raise _config_error(exc) from exc
    return _versioned_response(
        {"updated": True},
        _commit_revision(commit, manager.config_path),
    )


@router.put("/config/table")
async def update_secure_table_config(
    request: Request,
    _auth: dict[str, Any] = Depends(authenticate_admin),
):
    manager = _manager(request)
    try:
        expected = _expected_revision(request)
        commit = transactional_replace_config(
            manager.config_path,
            await _json_object(request),
            manager,
            expected_revision=expected,
        )
    except Exception as exc:
        raise _config_error(exc) from exc
    return _versioned_response(
        {"updated": True},
        _commit_revision(commit, manager.config_path),
    )


def _request_with_json(request: Request, body: dict[str, Any]) -> Request:
    payload = json.dumps(body).encode("utf-8")
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {
            "type": "http.request",
            "body": payload,
            "more_body": False,
        }

    return Request(request.scope, receive)


@router.post("/rag/documents")
async def import_secure_rag_document(
    request: Request,
    _auth: dict[str, Any] = Depends(authenticate_admin),
):
    body = await _json_object(request)
    url = body.get("url")
    if isinstance(url, str) and url.strip():
        try:
            content, filename = await fetch_public_text(url.strip())
        except HTTPException:
            raise
        except httpx.HTTPError as exc:
            raise HTTPException(
                400,
                detail={
                    "error": {
                        "code": "validation_error",
                        "message": "Failed to fetch URL",
                    }
                },
            ) from exc
        body = dict(body)
        body["content"] = content
        body["filename"] = body.get("filename") or filename
        body["url"] = ""

    from .admin_routes import import_rag_document

    return await import_rag_document(_request_with_json(request, body), _auth)


def _route_conflicts(existing: Any, secured_routes: list[Any]) -> bool:
    existing_path = getattr(existing, "path", None)
    existing_methods = set(getattr(existing, "methods", set()) or set())
    if not existing_path or not existing_methods:
        return False
    for secured in secured_routes:
        if getattr(secured, "path", None) != existing_path:
            continue
        secured_methods = set(getattr(secured, "methods", set()) or set())
        if existing_methods & secured_methods:
            return True
    return False


def install_security_routes(admin_router: APIRouter) -> None:
    marker = "_aigateway_security_routes_installed"
    if getattr(admin_router, marker, False):
        return
    secured_routes = list(router.routes)
    admin_router.routes[:] = [
        existing
        for existing in admin_router.routes
        if not _route_conflicts(existing, secured_routes)
    ]
    admin_router.routes[0:0] = secured_routes
    setattr(admin_router, marker, True)


_validate_public_url = validate_public_url
__all__ = ["install_security_routes", "router", "_validate_public_url"]
