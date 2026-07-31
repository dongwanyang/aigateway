from __future__ import annotations

import asyncio

import pytest
import yaml
from fastapi import HTTPException

from aigateway_api.config_security import (
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


def test_secure_routes_precede_legacy_admin_routes() -> None:
    from aigateway_api import admin_routes

    matching = [
        route
        for route in admin_routes.router.routes
        if getattr(route, "path", None) == "/config"
        and "GET" in getattr(route, "methods", set())
    ]

    assert matching
    assert matching[0].endpoint.__name__ == "get_secure_full_config"
