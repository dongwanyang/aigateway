"""Browser session endpoints.

The control panel exchanges an API key once for an HttpOnly, SameSite cookie.
JavaScript never persists or reads the secret after login.
"""
from __future__ import annotations

import os
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from .auth_middleware import SESSION_COOKIE_NAME, authenticate

router = APIRouter()


class CreateSessionRequest(BaseModel):
    api_key: str = Field(..., min_length=1)


def _is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or forwarded.split(",", 1)[0].strip() == "https"


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
    return {
        "data": {
            "authenticated": True,
            "key_prefix": key_data.get("key_prefix", body.api_key[:8]),
            "scopes": key_data.get("scopes", []),
        },
        "message": "success",
    }


@router.get("/session")
async def get_session(
    _auth: Dict[str, Any] = Depends(authenticate),
) -> Dict[str, Any]:
    return {
        "data": {
            "authenticated": True,
            "key_prefix": _auth.get("key_prefix", ""),
            "scopes": _auth.get("scopes", []),
        },
        "message": "success",
    }


@router.delete("/session")
async def delete_session(response: Response) -> Dict[str, Any]:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", samesite="strict")
    return {"data": {"authenticated": False}, "message": "success"}
