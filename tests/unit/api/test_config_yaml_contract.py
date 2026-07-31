from __future__ import annotations

import asyncio

import yaml

from aigateway_core.shared.config import ConfigManager


def test_safe_reload_preserves_state_when_yaml_is_malformed(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "server": {"host": "0.0.0.0", "port": 8000},
                "plugins": [],
                "providers": {},
                "observability": {"log_level": "info"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_GATEWAY_ENV", "production")
    manager = ConfigManager(str(path))

    path.write_text("server: [unterminated", encoding="utf-8")

    assert asyncio.run(manager.safe_reload()) is False
    assert manager.get("server.port") == 8000
