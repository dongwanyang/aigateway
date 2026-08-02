from __future__ import annotations

import aigateway_api  # noqa: F401 - installs the targeted CORS header extension
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient


def test_config_if_match_header_passes_cors_preflight() -> None:
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://panel.example"],
        allow_methods=["PUT"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    )

    response = TestClient(app).options(
        "/admin/config/table",
        headers={
            "Origin": "https://panel.example",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "if-match,content-type",
        },
    )

    assert response.status_code == 200
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "if-match" in allowed
