from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

from aigateway_api import security_routes
from aigateway_api.config_security import (
    ConfigValidationError,
    MASKED_SECRET,
    redact_config,
    restore_masked_values,
)


def _request(body: bytes, *, content_type: str = "application/json") -> Request:
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    app = SimpleNamespace(
        state=SimpleNamespace(config_manager=SimpleNamespace(config_path="unused"))
    )
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "PUT",
            "scheme": "http",
            "path": "/admin/config/table",
            "raw_path": b"/admin/config/table",
            "query_string": b"",
            "headers": [
                (b"content-type", content_type.encode()),
                (b"if-match", b'"revision"'),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "app": app,
        },
        receive,
    )


def test_redacts_additional_secret_names_and_scalar_api_keys() -> None:
    safe = redact_config(
        {
            "infrastructure": {
                "object_store": {
                    "secret_access_key": "aws-secret",
                    "signing_key": "signing-secret",
                }
            },
            "auth": {"api_keys": ["first-secret", "second-secret"]},
        }
    )

    assert safe["infrastructure"]["object_store"]["secret_access_key"] == MASKED_SECRET
    assert safe["infrastructure"]["object_store"]["signing_key"] == MASKED_SECRET
    assert safe["auth"]["api_keys"] == [MASKED_SECRET, MASKED_SECRET]


def test_masked_scalar_list_does_not_restore_by_position() -> None:
    current = {"auth": {"api_keys": ["first-secret", "second-secret"]}}
    candidate = {"auth": {"api_keys": [MASKED_SECRET, MASKED_SECRET]}}

    with pytest.raises(ConfigValidationError, match="unambiguous persisted value"):
        restore_masked_values(candidate, current)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [b"{", json.dumps(["not", "an", "object"]).encode("utf-8")],
)
async def test_config_route_preserves_json_validation_as_http_400(payload: bytes) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await security_routes.update_secure_table_config(_request(payload), {})

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"]["code"] == "validation_error"
