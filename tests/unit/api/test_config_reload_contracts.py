from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import yaml

from aigateway_api import security_routes
from aigateway_api.config_security import (
    ConfigCommit,
    ConfigValidationError,
    config_revision,
    transactional_replace_config,
)
from aigateway_core.pipelines.generation._common.config import (
    GenerationOptimizationConfigWatcher,
)
from aigateway_core.shared.config import ConfigManager


def _base_config() -> dict:
    return {
        "server": {"host": "0.0.0.0", "port": 8000},
        "plugins": [],
        "providers": {},
        "observability": {"log_level": "info"},
    }


def test_transaction_returns_exact_locked_revision(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_base_config()), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_ENV", "production")
    manager = ConfigManager(str(path))
    candidate = _base_config()
    candidate["server"]["port"] = 9000

    commit = transactional_replace_config(str(path), candidate, manager)

    assert isinstance(commit, ConfigCommit)
    assert commit.revision == config_revision(str(path))
    assert commit["server"]["port"] == 9000


def test_route_uses_commit_revision_without_rereading(monkeypatch) -> None:
    commit = ConfigCommit({}, "locked-revision")

    def fail_read(_path: str) -> str:
        raise AssertionError("revision must not be reread after lock release")

    monkeypatch.setattr(security_routes, "config_revision", fail_read)

    assert security_routes._commit_revision(commit, "/missing") == (
        "locked-revision"
    )


def test_full_reload_resets_removed_integration_field(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "config.yaml"
    initial = _base_config()
    initial["plugins"] = [
        {
            "name": "rag_retriever",
            "enabled": True,
            "config": {"top_k": 9},
        }
    ]
    path.write_text(yaml.safe_dump(initial), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_ENV", "production")
    manager = ConfigManager(str(path))
    assert manager.integration_configs.rag_retriever.top_k == 9

    updated = _base_config()
    updated["plugins"] = [
        {"name": "rag_retriever", "enabled": True, "config": {}}
    ]
    path.write_text(yaml.safe_dump(updated), encoding="utf-8")

    manager.load()

    assert manager.integration_configs.rag_retriever.top_k == 5


def test_safe_reload_rejects_invalid_component_and_preserves_state(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "config.yaml"
    initial = _base_config()
    initial["generation_optimization"] = {
        "draft_workflow": {"store_dir": "/data/drafts"}
    }
    path.write_text(yaml.safe_dump(initial), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_ENV", "production")
    manager = ConfigManager(str(path))

    invalid = _base_config()
    invalid["generation_optimization"] = {
        "draft_workflow": {"store_dir": 123}
    }
    path.write_text(yaml.safe_dump(invalid), encoding="utf-8")

    assert asyncio.run(manager.safe_reload()) is False
    assert manager.get("generation_optimization.draft_workflow.store_dir") == (
        "/data/drafts"
    )


def test_transaction_rejects_numeric_draft_store_dir(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_base_config()), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_ENV", "production")
    manager = ConfigManager(str(path))
    candidate = _base_config()
    candidate["generation_optimization"] = {
        "draft_workflow": {"store_dir": 123}
    }

    with pytest.raises(ConfigValidationError) as exc_info:
        transactional_replace_config(str(path), candidate, manager)

    assert "store_dir" in str(exc_info.value.issues)


class _FakeConfigManager:
    def __init__(self) -> None:
        self._config = {
            "generation_optimization": {"enabled": True}
        }
        self.callbacks = []

    def get(self, path: str, default=None):
        if path == "generation_optimization":
            return self._config["generation_optimization"]
        return default

    def snapshot(self):
        return self._config

    def on_reload(self, callback) -> None:
        self.callbacks.append(callback)


def test_generation_watcher_rolls_back_failed_runtime_callback() -> None:
    manager = _FakeConfigManager()
    watcher = GenerationOptimizationConfigWatcher(manager)

    def reject_disabled(config) -> None:
        if config.enabled is False:
            raise RuntimeError("runtime rejected disabled state")

    watcher.on_change(reject_disabled)

    with pytest.raises(RuntimeError, match="runtime rejected"):
        manager.callbacks[0](
            {"generation_optimization": {"enabled": False}}
        )

    assert watcher.config.enabled is True
