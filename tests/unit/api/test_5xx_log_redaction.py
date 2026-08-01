from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from aigateway_api.main import _register_exception_handlers


def test_5xx_logs_do_not_include_raw_exception_secrets(caplog) -> None:
    app = FastAPI()
    _register_exception_handlers(app)
    secret = "redis://user:super-secret@example:6379/0"

    @app.get("/unhandled")
    async def unhandled() -> None:
        raise RuntimeError(secret)

    @app.get("/http")
    async def http_error() -> None:
        raise HTTPException(
            500,
            detail={
                "error": {
                    "code": "upstream_failed",
                    "message": secret,
                }
            },
        )

    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR):
        assert client.get("/unhandled").status_code == 500
        assert client.get("/http").status_code == 500

    assert secret not in caplog.text
    assert "super-secret" not in caplog.text
    assert "RuntimeError" in caplog.text
    assert "upstream_failed" in caplog.text
