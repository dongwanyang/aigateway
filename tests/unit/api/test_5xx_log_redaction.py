from __future__ import annotations

import logging

import httpx
import pytest
from aigateway_api.main import _register_exception_handlers
from fastapi import FastAPI, HTTPException


@pytest.mark.asyncio
async def test_5xx_logs_do_not_include_raw_exception_secrets(caplog) -> None:
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

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        with caplog.at_level(logging.ERROR):
            assert (await client.get("/unhandled")).status_code == 500
            assert (await client.get("/http")).status_code == 500

    assert secret not in caplog.text
    assert "super-secret" not in caplog.text
    assert "RuntimeError" in caplog.text
    assert "upstream_failed" in caplog.text
