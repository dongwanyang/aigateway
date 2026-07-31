from __future__ import annotations

import yaml

from aigateway_core.shared.config_env import apply_env_overrides
from aigateway_core.shared.configured_config import ConfigManager


def test_environment_names_resolve_to_nested_yaml_paths(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "server": {
                    "port": 8000,
                    "cors_origins": ["http://old"],
                },
                "observability": {"log_level": "info"},
                "infrastructure": {
                    "redis": {"url": "redis://old"},
                    "qdrant": {"url": "http://old:6333"},
                },
                "plugins": [],
                "providers": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_GATEWAY_SERVER_PORT", "9100")
    monkeypatch.setenv(
        "AI_GATEWAY_OBSERVABILITY_LOG_LEVEL",
        "warning",
    )
    monkeypatch.setenv(
        "AI_GATEWAY_QDRANT_URL",
        "http://qdrant:6333",
    )
    monkeypatch.setenv(
        "AI_GATEWAY_CORS_ORIGINS",
        "https://one.example,https://two.example",
    )

    manager = ConfigManager(str(path))

    assert manager.get("server.port") == 9100
    assert manager.get("observability.log_level") == "warning"
    assert manager.get("infrastructure.qdrant.url") == (
        "http://qdrant:6333"
    )
    assert manager.get("server.cors_origins") == [
        "https://one.example",
        "https://two.example",
    ]
    assert "server_port" not in manager.snapshot()


def test_explicit_double_underscore_path_is_supported() -> None:
    config = {"custom": {"nested_value": "old"}}

    updated, applied = apply_env_overrides(
        config,
        environ={"AI_GATEWAY_CUSTOM__NESTED_VALUE": "new"},
    )

    assert updated["custom"]["nested_value"] == "new"
    assert applied == [
        ("AI_GATEWAY_CUSTOM__NESTED_VALUE", "custom.nested_value")
    ]


def test_low_level_runtime_values_observe_environment_overrides(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "infrastructure": {
                    "qdrant": {"url": "http://yaml:6333"},
                    "redis": {"namespace": "gateway"},
                },
                "observability": {"otel_service_name": "gateway"},
                "cache": {"pipeline_version": "2"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", str(path))
    monkeypatch.setenv("AI_GATEWAY_QDRANT_URL", "http://env:6333")

    from aigateway_core.shared import runtime_values

    assert runtime_values.configured_text(
        "infrastructure.qdrant.url"
    ) == "http://env:6333"


def test_draft_store_dir_uses_environment_value(monkeypatch) -> None:
    from aigateway_core.pipelines.generation._common.config import (
        parse_generation_optimization_config,
    )

    monkeypatch.setenv(
        "AI_GATEWAY_GENERATION_OPTIMIZATION_DRAFT_WORKFLOW_STORE_DIR",
        "/data/env-drafts",
    )

    config = parse_generation_optimization_config(
        {"draft_workflow": {"store_dir": "/data/yaml-drafts"}}
    )

    assert config.draft_workflow.store_dir == "/data/env-drafts"
