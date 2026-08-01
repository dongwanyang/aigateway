from __future__ import annotations

import aigateway_api
import yaml
from aigateway_core.shared.config import ConfigManager


def test_yaml_cors_bootstrap_does_not_become_runtime_env_override(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "server": {
                    "port": 8000,
                    "cors_origins": ["https://one.example"],
                },
                "plugins": [],
                "providers": {},
                "observability": {"log_level": "info"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", str(path))
    monkeypatch.setenv("AI_GATEWAY_ENV", "production")
    monkeypatch.delenv("AI_GATEWAY_CORS_ORIGINS", raising=False)
    monkeypatch.delenv(
        "AI_GATEWAY_CORS_ORIGINS_BOOTSTRAPPED_FROM_YAML",
        raising=False,
    )
    monkeypatch.setattr(aigateway_api, "_dotenv_bootstrap_values", dict)

    aigateway_api._preload_cors_origins()

    assert aigateway_api.os.environ["AI_GATEWAY_CORS_ORIGINS"] == (
        "https://one.example"
    )
    assert aigateway_api.os.environ[
        "AI_GATEWAY_CORS_ORIGINS_BOOTSTRAPPED_FROM_YAML"
    ] == "1"

    manager = ConfigManager(str(path))
    assert "AI_GATEWAY_CORS_ORIGINS" not in aigateway_api.os.environ
    assert manager.get("server.cors_origins") == ["https://one.example"]

    updated = yaml.safe_load(path.read_text(encoding="utf-8"))
    updated["server"]["cors_origins"] = ["https://two.example"]
    path.write_text(yaml.safe_dump(updated), encoding="utf-8")
    manager.load()

    assert manager.get("server.cors_origins") == ["https://two.example"]
