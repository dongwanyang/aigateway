from __future__ import annotations

import yaml
import pytest

from aigateway_api.config_security import (
    ConfigVersionConflictError,
    config_revision,
    transactional_replace_config,
)
from aigateway_core.shared.config import ConfigManager


def _config(port: int) -> dict:
    return {
        "server": {"host": "0.0.0.0", "port": port},
        "plugins": [],
        "providers": {},
        "observability": {"log_level": "info"},
    }


def test_transaction_detects_external_write_during_runtime_reload(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_config(8000)), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_ENV", "production")
    manager = ConfigManager(str(path))
    expected = config_revision(str(path))
    real_load = manager.load
    external = _config(8100)
    calls = 0

    def load_then_external_write():
        nonlocal calls
        calls += 1
        loaded = real_load()
        if calls == 1:
            path.write_text(yaml.safe_dump(external), encoding="utf-8")
        return loaded

    monkeypatch.setattr(manager, "load", load_then_external_write)

    with pytest.raises(ConfigVersionConflictError):
        transactional_replace_config(
            str(path),
            _config(8200),
            manager,
            expected_revision=expected,
        )

    assert yaml.safe_load(path.read_text(encoding="utf-8"))["server"]["port"] == 8100
    assert manager.get("server.port") == 8100
