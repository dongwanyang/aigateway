"""Expose deployed source identity through the public health response."""
from __future__ import annotations

import json
import os
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse


def deployed_commit_sha() -> str:
    for name in (
        "AIGATEWAY_COMMIT_SHA",
        "GIT_COMMIT_SHA",
        "SOURCE_VERSION",
        "RENDER_GIT_COMMIT",
        "IMAGE_COMMIT_SHA",
    ):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return "unknown"


def install_runtime_identity(router: APIRouter) -> None:
    marker = "_aigateway_runtime_identity_installed"
    if getattr(router, marker, False):
        return
    for route in router.routes:
        if getattr(route, "path", None) != "/health":
            continue
        original = getattr(route, "endpoint", None)
        if original is None:
            continue

        async def health_with_identity(*args: Any, __original=original, **kwargs: Any):
            response = await __original(*args, **kwargs)
            if not isinstance(response, JSONResponse):
                return response
            try:
                payload = json.loads(response.body.decode("utf-8"))
            except (AttributeError, UnicodeDecodeError, ValueError):
                return response
            data = payload.get("data")
            if isinstance(data, dict):
                data["commit_sha"] = deployed_commit_sha()
                data["image_version"] = os.getenv("AIGATEWAY_IMAGE_VERSION", "")
            headers = {
                key: value
                for key, value in response.headers.items()
                if key.lower() not in {"content-length", "content-type"}
            }
            return JSONResponse(
                content=payload,
                status_code=response.status_code,
                headers=headers,
            )

        route.endpoint = health_with_identity
        dependant = getattr(route, "dependant", None)
        if dependant is not None:
            dependant.call = health_with_identity
        break
    setattr(router, marker, True)


__all__ = ["deployed_commit_sha", "install_runtime_identity"]
