from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest
import yaml
from fastapi import HTTPException, Request

from aigateway_api import config_security, security_routes
from aigateway_api.config_security import (
    ConfigValidationError,
    ConfigVersionConflictError,
    MASKED_SECRET,
    config_revision,
    redact_config,
    restore_masked_values,
    transactional_replace_config,
)
from aigateway_core.shared.config import ConfigManager


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


def _minimal_config(port: int = 8000) -> dict:
    return {
        "server": {"host": "0.0.0.0", "port": port},
        "plugins": [],
        "providers": {},
        "observability": {"log_level": "info"},
    }


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

    with pytest.raises(ConfigValidationError) as exc_info:
        restore_masked_values(candidate, current)

    assert "unambiguous persisted value" in str(exc_info.value.issues)


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


def test_full_reload_transactions_are_serialized(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_minimal_config()), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_ENV", "production")
    manager = ConfigManager(str(path))

    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def callback(_config: dict) -> None:
        nonlocal calls
        with calls_lock:
            calls += 1
            current = calls
        if current == 1:
            first_entered.set()
            assert release_first.wait(2)
        else:
            second_entered.set()

    manager.on_reload(callback)
    first = threading.Thread(target=manager.load)
    second = threading.Thread(target=manager.load)
    first.start()
    assert first_entered.wait(2)
    second.start()

    assert not second_entered.wait(0.1)
    release_first.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert second_entered.is_set()


def test_transaction_rechecks_revision_immediately_before_replace(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_minimal_config()), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_ENV", "production")
    manager = ConfigManager(str(path))
    expected = config_revision(str(path))
    original_validate = config_security.validate_candidate

    def validate_then_external_write(selected_manager, candidate):
        result = original_validate(selected_manager, candidate)
        path.write_text(yaml.safe_dump(_minimal_config(8100)), encoding="utf-8")
        return result

    monkeypatch.setattr(
        config_security,
        "validate_candidate",
        validate_then_external_write,
    )

    with pytest.raises(ConfigVersionConflictError):
        transactional_replace_config(
            str(path),
            _minimal_config(8200),
            manager,
            expected_revision=expected,
        )

    assert yaml.safe_load(path.read_text(encoding="utf-8"))["server"]["port"] == 8100


def test_transaction_rejects_component_fields_that_runtime_would_ignore(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_minimal_config()), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_ENV", "production")
    manager = ConfigManager(str(path))
    candidate = _minimal_config()
    candidate["plugins"] = [
        {
            "name": "rag_retriever",
            "enabled": True,
            "config": {"top_kk": 9},
        }
    ]

    with pytest.raises(ConfigValidationError) as exc_info:
        transactional_replace_config(str(path), candidate, manager)

    assert "top_kk" in str(exc_info.value.issues)
    assert "unknown field" in str(exc_info.value.issues)
