from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from aigateway_api.exception_sanitizer import install_exception_response_sanitizer


def test_exception_handler_never_returns_original_5xx_text() -> None:
    install_exception_response_sanitizer()
    app = FastAPI()

    @app.exception_handler(RuntimeError)
    async def runtime_error_handler(request: Request, exc: RuntimeError):
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": str(exc),
                    "detail": "redis://user:secret@example.internal/0",
                }
            },
            headers={"X-Request-ID": "req-test"},
        )

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("provider returned sk-secret-value")

    response = TestClient(app, raise_server_exceptions=False).get("/boom")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "Internal Server Error",
            "request_id": "req-test",
        }
    }
    assert "secret" not in response.text
