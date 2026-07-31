"""Install a fail-closed wrapper around FastAPI exception handlers."""

from __future__ import annotations

from functools import wraps
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

_INSTALLED = False


def install_exception_response_sanitizer() -> None:
    """Remove exception text from every 5xx response registered afterwards.

    The application keeps logging the complete exception server-side. Clients
    receive only a stable error code and request ID, preventing credentials,
    connection strings, paths, and provider responses from leaking through a
    partially effective regular-expression redactor.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original = FastAPI.exception_handler

    def exception_handler(self: FastAPI, exc_class_or_status_code: Any):
        register = original(self, exc_class_or_status_code)

        def decorator(
            handler: Callable[[Request, Any], Awaitable[Response]],
        ) -> Callable[[Request, Any], Awaitable[Response]]:
            @wraps(handler)
            async def sanitized(request: Request, exc: Any) -> Response:
                response = await handler(request, exc)
                if response.status_code < 500:
                    return response
                request_id = response.headers.get("X-Request-ID") or getattr(
                    request.state,
                    "request_id",
                    "",
                )
                headers = dict(response.headers)
                headers.pop("content-length", None)
                if request_id:
                    headers["X-Request-ID"] = str(request_id)
                return JSONResponse(
                    status_code=response.status_code,
                    content={
                        "error": {
                            "code": "internal_error",
                            "message": "Internal Server Error",
                            **({"request_id": str(request_id)} if request_id else {}),
                        }
                    },
                    headers=headers,
                )

            return register(sanitized)

        return decorator

    FastAPI.exception_handler = exception_handler
