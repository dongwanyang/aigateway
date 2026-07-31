from __future__ import annotations

import asyncio
import json
import socket
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi import APIRouter, HTTPException, Request

from aigateway_api import admin_routes, safe_http, security_routes
from aigateway_api.config_security import (
    ConfigUpdateBusyError,
    ConfigValidationError,
    MASKED_SECRET,
    redact_config,
    restore_masked_values,
    transactional_replace_config,
)
from aigateway_api.security_routes import _validate_public_url
from aigateway_core.shared.configured_config import ConfigManager


def _config() -> dict:
    return {
        "server": {"host": "0.0.0.0", "port": 8000},
        "auth": {
            "api_keys": [
                {"key": "gw-secret", "user_id": "admin"}
            ],
        },
        "plugins": [
            {
                "name": "rag_retriever",
                "enabled": True,
                "config": {
                    "embedding_api_key": "plugin-secret"
                },
            }
        ],
        "providers": {
            "openai": {"api_key": "provider-secret"}
        },
        "infrastructure": {
            "redis": {
                "url": "redis://user:password@redis:6379/0"
            }
        },
        "observability": {"log_level": "info"},
    }


def _request(
    body: object = None,
    *,
    app: object | None = None,
    invalid_json: bool = False,
) -> Request:
    payload = b"{" if invalid_json else json.dumps(body).encode("utf-8")
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {
            "type": "http.request",
            "body": payload,
            "more_body": False,
        }

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "PUT",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "app": app or SimpleNamespace(state=SimpleNamespace()),
        },
        receive,
    )


def _public_dns(*_args, **_kwargs):
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("93.184.216.34", 443),
        )
    ]


def test_recursive_redaction_and_mask_restoration() -> None:
    current = _config()

    safe = redact_config(current)

    assert safe["auth"]["api_keys"][0]["key"] == MASKED_SECRET
    assert (
        safe["plugins"][0]["config"]["embedding_api_key"]
        == MASKED_SECRET
    )
    assert (
        safe["providers"]["openai"]["api_key"]
        == MASKED_SECRET
    )
    assert (
        safe["infrastructure"]["redis"]["url"]
        == MASKED_SECRET
    )
    assert restore_masked_values(safe, current) == current


def test_mask_restoration_matches_named_items_after_reordering() -> None:
    current = {
        "plugins": [
            {"name": "one", "config": {"api_key": "secret-one"}},
            {"name": "two", "config": {"api_key": "secret-two"}},
        ]
    }
    safe = redact_config(current)
    safe["plugins"].reverse()

    restored = restore_masked_values(safe, current)

    assert restored["plugins"][0]["config"]["api_key"] == "secret-two"
    assert restored["plugins"][1]["config"]["api_key"] == "secret-one"


def test_invalid_candidate_never_replaces_config_file(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(_config(), sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_GATEWAY_ENV", "production")
    monkeypatch.setenv("AI_GATEWAY_SERVER_PORT", "9000")
    manager = ConfigManager(str(path))
    before = path.read_bytes()
    candidate = _config()
    candidate["server"]["port"] = "not-a-port"

    with pytest.raises(ConfigValidationError):
        transactional_replace_config(str(path), candidate, manager)

    assert path.read_bytes() == before
    assert manager.get("server.port") == 9000


def test_valid_candidate_is_committed_and_reloaded(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(_config(), sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_GATEWAY_ENV", "production")
    manager = ConfigManager(str(path))
    candidate = _config()
    candidate["server"]["port"] = 9001

    saved = transactional_replace_config(str(path), candidate, manager)

    assert saved["server"]["port"] == 9001
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["server"]["port"] == 9001
    assert manager.get("server.port") == 9001


def test_reload_failure_rolls_back_persisted_bytes(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(_config(), sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_GATEWAY_ENV", "production")
    manager = ConfigManager(str(path))
    before = path.read_bytes()
    candidate = _config()
    candidate["server"]["port"] = 9000

    def fail_load():
        raise RuntimeError("reload failed")

    monkeypatch.setattr(manager, "load", fail_load)
    with pytest.raises(RuntimeError, match="reload failed"):
        transactional_replace_config(str(path), candidate, manager)

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://10.0.0.1/internal",
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
        "http://user:password@example.com/",
    ],
)
def test_rag_url_validation_rejects_private_or_unsafe_targets(
    url: str,
) -> None:
    with pytest.raises(HTTPException):
        asyncio.run(_validate_public_url(url))


@pytest.mark.asyncio
async def test_rag_url_validation_accepts_public_dns(monkeypatch) -> None:
    monkeypatch.setattr(safe_http.socket, "getaddrinfo", _public_dns)

    await safe_http.validate_public_url("https://example.com/document")


@pytest.mark.asyncio
async def test_rag_url_validation_rejects_unresolvable_dns(monkeypatch) -> None:
    def fail_dns(*_args, **_kwargs):
        raise socket.gaierror("not found")

    monkeypatch.setattr(safe_http.socket, "getaddrinfo", fail_dns)

    with pytest.raises(HTTPException) as exc_info:
        await safe_http.validate_public_url("https://missing.example/document")
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_safe_fetch_follows_validated_redirect_and_reads_text(
    monkeypatch,
) -> None:
    monkeypatch.setattr(safe_http.socket, "getaddrinfo", _public_dns)
    real_async_client = httpx.AsyncClient
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            content=b"safe document",
        )

    def client_factory(**_kwargs):
        return real_async_client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )

    monkeypatch.setattr(safe_http.httpx, "AsyncClient", client_factory)

    text, filename = await safe_http.fetch_public_text(
        "https://example.com/start"
    )

    assert text == "safe document"
    assert filename == "final"
    assert requests == [
        "https://example.com/start",
        "https://example.com/final",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "status_code"),
    [
        ({"content-type": "application/octet-stream"}, 400),
        (
            {
                "content-type": "text/plain",
                "content-length": str(safe_http.MAX_RESPONSE_BYTES + 1),
            },
            413,
        ),
    ],
)
async def test_safe_fetch_rejects_unsupported_or_declared_large_response(
    monkeypatch,
    headers,
    status_code,
) -> None:
    monkeypatch.setattr(safe_http.socket, "getaddrinfo", _public_dns)
    real_async_client = httpx.AsyncClient

    def client_factory(**_kwargs):
        return real_async_client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, headers=headers, content=b"x")
            )
        )

    monkeypatch.setattr(safe_http.httpx, "AsyncClient", client_factory)

    with pytest.raises(HTTPException) as exc_info:
        await safe_http.fetch_public_text("https://example.com/document")
    assert exc_info.value.status_code == status_code


def test_config_error_mapping_preserves_status_and_details() -> None:
    busy = security_routes._config_error(ConfigUpdateBusyError("busy"))
    invalid = security_routes._config_error(
        ConfigValidationError(
            [{"level": "ERROR", "message": "server.port invalid"}]
        )
    )
    unknown = security_routes._config_error(RuntimeError("disk error"))

    assert busy.status_code == 409
    assert invalid.status_code == 422
    assert invalid.detail["error"]["issues"][0]["level"] == "ERROR"
    assert unknown.status_code == 500


@pytest.mark.asyncio
async def test_json_object_rejects_invalid_and_non_object_payloads() -> None:
    with pytest.raises(HTTPException) as invalid:
        await security_routes._json_object(_request(invalid_json=True))
    with pytest.raises(HTTPException) as non_object:
        await security_routes._json_object(_request(["not", "object"]))

    assert invalid.value.status_code == 400
    assert non_object.value.status_code == 400


@pytest.mark.asyncio
async def test_secure_config_routes_redact_and_delegate_transaction(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_config()), encoding="utf-8")
    manager = SimpleNamespace(config_path=str(path))
    app = SimpleNamespace(state=SimpleNamespace(config_manager=manager))
    captured: list[dict] = []

    def fake_transaction(_path, candidate, selected_manager):
        captured.append(candidate)
        assert selected_manager is manager
        return candidate

    monkeypatch.setattr(
        security_routes,
        "transactional_replace_config",
        fake_transaction,
    )

    loaded = await security_routes.get_secure_full_config(
        _request({}, app=app),
        {},
    )
    updated = await security_routes.update_secure_full_config(
        _request({"server": {"host": "127.0.0.1", "port": 9000}}, app=app),
        {},
    )
    table = await security_routes.update_secure_table_config(
        _request(_config(), app=app),
        {},
    )

    assert loaded["data"]["providers"]["openai"]["api_key"] == MASKED_SECRET
    assert updated["data"]["updated"] is True
    assert table["data"]["updated"] is True
    assert captured[0]["server"]["port"] == 9000
    assert captured[1]["server"]["port"] == 8000


@pytest.mark.asyncio
async def test_secure_rag_route_prefetches_url_before_delegating(
    monkeypatch,
) -> None:
    delegated: dict = {}

    async def fake_fetch(url: str):
        assert url == "https://example.com/document.txt"
        return "document body", "document.txt"

    async def fake_import(request: Request, auth: dict):
        delegated.update(await request.json())
        assert auth == {"scope": "admin"}
        return {"data": {"doc_id": "doc-1"}, "message": "success"}

    monkeypatch.setattr(security_routes, "fetch_public_text", fake_fetch)
    monkeypatch.setattr(admin_routes, "import_rag_document", fake_import)

    result = await security_routes.import_secure_rag_document(
        _request({"url": "https://example.com/document.txt"}),
        {"scope": "admin"},
    )

    assert result["data"]["doc_id"] == "doc-1"
    assert delegated["url"] == ""
    assert delegated["content"] == "document body"
    assert delegated["filename"] == "document.txt"


def test_secure_routes_precede_legacy_admin_routes() -> None:
    matching = [
        route
        for route in admin_routes.router.routes
        if getattr(route, "path", None) == "/config"
        and "GET" in getattr(route, "methods", set())
    ]

    assert matching
    assert matching[0].endpoint.__name__ == "get_secure_full_config"


def test_security_route_installation_is_idempotent() -> None:
    target = APIRouter()

    security_routes.install_security_routes(target)
    first_count = len(target.routes)
    security_routes.install_security_routes(target)

    assert first_count == len(security_routes.router.routes)
    assert len(target.routes) == first_count
