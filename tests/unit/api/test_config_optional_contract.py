from __future__ import annotations

import yaml

from aigateway_api.config_security import transactional_replace_config
from aigateway_core.shared.config import ConfigManager


def test_optional_integration_fields_accept_explicit_null(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.yaml"
    base = {
        "server": {"host": "0.0.0.0", "port": 8000},
        "plugins": [],
        "providers": {},
        "observability": {"log_level": "info"},
    }
    path.write_text(yaml.safe_dump(base), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_ENV", "production")
    manager = ConfigManager(str(path))
    candidate = dict(base)
    candidate["plugins"] = [
        {
            "name": "rag_retriever",
            "enabled": True,
            "config": {
                "rerank_api_base": None,
                "rerank_api_key": None,
                "embedding_api_base": None,
                "embedding_api_key": None,
            },
        }
    ]

    transactional_replace_config(str(path), candidate, manager)

    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = saved["plugins"][0]["config"]
    assert config["rerank_api_key"] is None
    assert config["embedding_api_key"] is None
