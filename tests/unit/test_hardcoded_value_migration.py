"""Regression tests for config-backed runtime values."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
import yaml


def _write_config(tmp_path, data: dict) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return str(path)


def _base_config() -> dict:
    return {
        "auth": {
            "database_path": "data/auth.db",
            "database_timeout_seconds": 3,
            "admin_username": "console-admin",
            "admin_user_id": "operator-1",
            "session": {
                "idle_ttl_seconds": 1200,
                "absolute_ttl_seconds": 7200,
            },
            "password": {"pbkdf2_iterations": 1000},
        },
        "observability": {"otel_service_name": "test-gateway"},
        "infrastructure": {
            "redis": {
                "namespace": "tenant-a",
                "key_prefixes": {"media": "custom:media:"},
            },
            "qdrant": {
                "url": "${TEST_QDRANT_URL:-http://configured-qdrant:6333}",
                "connect_timeout": 7,
                "read_timeout": 11,
                "write_timeout": 13,
                "distance": "DOT",
                "hnsw_m": 32,
                "hnsw_ef_construct": 256,
            },
        },
        "embedding": {"vector_dim": 768},
        "cache": {
            "pipeline_version": "9",
            "key_buckets": {"max_tokens": [100, 200, 400]},
        },
        "media_optimization": {"media_cache_ttl": 123},
        "generation_optimization": {
            "preset_store_dir": "data/generation-presets",
        },
        "providers": {
            "demo": {
                "model_grouper": [
                    {
                        "models": [
                            {"name": "demo-model"},
                            {"name": "free-model"},
                        ],
                        "pricing": {
                            "demo-model": {
                                "prompt": 0.002,
                                "completion": 0.004,
                            },
                            "free-model": {
                                "prompt": 0.0,
                                "completion": 0.0,
                            },
                        },
                    }
                ]
            }
        },
    }


def test_runtime_values_use_yaml_namespace_and_explicit_prefix(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "AI_GATEWAY_CONFIG_PATH", _write_config(tmp_path, _base_config())
    )

    from aigateway_core.shared.runtime_values import (
        configured_text,
        media_cache_ttl_seconds,
        redis_key_prefix,
    )

    assert redis_key_prefix("media") == "custom:media"
    assert redis_key_prefix("l2_index") == "tenant-a:l2:idx:v9"
    assert redis_key_prefix("l2_hash") == "tenant-a:cache:v9search"
    assert redis_key_prefix("prompt_template") == "tenant-a:prompt_template"
    assert media_cache_ttl_seconds() == 123
    assert (
        configured_text("infrastructure.qdrant.url")
        == "http://configured-qdrant:6333"
    )

    monkeypatch.setenv("TEST_QDRANT_URL", "https://qdrant.example")
    assert (
        configured_text("infrastructure.qdrant.url")
        == "https://qdrant.example"
    )


def test_configured_relative_path_is_anchored_to_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "AI_GATEWAY_CONFIG_PATH", _write_config(tmp_path, _base_config())
    )
    monkeypatch.delenv("AI_GATEWAY_AUTH_DB_PATH", raising=False)

    from aigateway_core.shared.auth.sqlite_store import SQLiteStore

    store = SQLiteStore()
    expected = tmp_path / "data" / "auth.db"
    assert Path(store.db_path) == expected.resolve()
    assert expected.is_file()
    store.conn.close()


def test_browser_auth_policy_is_loaded_from_config(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "AI_GATEWAY_CONFIG_PATH", _write_config(tmp_path, _base_config())
    )
    for name in (
        "AI_GATEWAY_ADMIN_USERNAME",
        "AI_GATEWAY_ADMIN_USER_ID",
        "AI_GATEWAY_SESSION_TTL_SECONDS",
        "AI_GATEWAY_SESSION_ABSOLUTE_TTL_SECONDS",
        "AI_GATEWAY_PASSWORD_PBKDF2_ITERATIONS",
    ):
        monkeypatch.delenv(name, raising=False)

    from aigateway_api import auth_routes
    from aigateway_api.browser_auth import BrowserAuthStore

    assert auth_routes._admin_username() == "console-admin"
    assert auth_routes._session_ttl() == 1200
    assert auth_routes._absolute_session_ttl() == 7200

    store = BrowserAuthStore(str(tmp_path / "browser.db"))
    user = store.provision_admin("console-admin", "temporary-password")
    assert user is not None
    assert user["user_id"] == "operator-1"
    assert str(user["password_hash"]).startswith("pbkdf2_sha256$1000$")
    assert store._timeout_seconds == 3.0


def test_generation_preset_directory_is_loaded_from_config(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "AI_GATEWAY_CONFIG_PATH", _write_config(tmp_path, _base_config())
    )
    monkeypatch.delenv("AI_GATEWAY_GENERATION_PRESETS_DIR", raising=False)

    from aigateway_api.local_generation import preset_store_dir

    assert preset_store_dir() == (
        tmp_path / "data" / "generation-presets"
    ).resolve()


def test_media_cache_manager_uses_configured_prefix_and_ttl(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "AI_GATEWAY_CONFIG_PATH", _write_config(tmp_path, _base_config())
    )

    from aigateway_core.prefix.media.cache import MediaCacheManager

    class FakeRedis:
        redis = None

    manager = MediaCacheManager(FakeRedis())
    assert manager._key_prefix == "custom:media"
    assert manager._default_ttl == 123


def test_l2_operations_refresh_prefixes_lazily(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "AI_GATEWAY_CONFIG_PATH", _write_config(tmp_path, _base_config())
    )

    from aigateway_core.prefix.cache import l2_search

    result = asyncio.run(l2_search.ensure_index(None))
    assert result is False
    assert l2_search.L2_INDEX_NAME == "tenant-a:l2:idx:v9"
    assert l2_search.L2_HASH_PREFIX == "tenant-a:cache:v9search:"


def test_max_token_buckets_are_loaded_from_config(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "AI_GATEWAY_CONFIG_PATH", _write_config(tmp_path, _base_config())
    )

    from aigateway_core.prefix.cache.cache_keys import _bucket_max_tokens

    assert _bucket_max_tokens(50) == "le_100"
    assert _bucket_max_tokens(150) == "le_200"
    assert _bucket_max_tokens(300) == "le_400"
    assert _bucket_max_tokens(500) == "gt_400"


def test_prompt_template_manager_resolves_instance_prefixes(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "AI_GATEWAY_CONFIG_PATH", _write_config(tmp_path, _base_config())
    )

    from aigateway_core.pipelines.generation._common.config import (
        PromptTemplateConfig,
    )
    from aigateway_core.pipelines.generation.token import PromptTemplateManager

    manager = PromptTemplateManager(None, PromptTemplateConfig())
    assert manager.KEY_PREFIX == "tenant-a:prompt_template"
    assert manager.INDEX_PREFIX == "tenant-a:prompt_template_index"


def test_qdrant_manager_uses_configured_connection_and_index_values(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "AI_GATEWAY_CONFIG_PATH", _write_config(tmp_path, _base_config())
    )
    monkeypatch.delenv("TEST_QDRANT_URL", raising=False)

    import aigateway_core.shared.qdrant_client as qdrant_module
    from aigateway_core.shared.qdrant_client import QdrantClientManager

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeAsyncClient:
        instances = []

        def __init__(self, *, base_url, timeout):
            self.base_url = base_url
            self.timeout = timeout
            self.put_calls = []
            self.__class__.instances.append(self)

        async def get(self, path):
            if path == "/collections/":
                return FakeResponse({"result": {"collections": []}})
            return FakeResponse({})

        async def put(self, path, *, json, headers):
            self.put_calls.append((path, json, headers))
            return FakeResponse({"result": {}})

        async def aclose(self):
            return None

    monkeypatch.setattr(qdrant_module, "AsyncClient", FakeAsyncClient)

    manager = QdrantClientManager()
    assert manager.url == ""
    asyncio.run(manager.connect())
    assert manager.url == "http://configured-qdrant:6333"

    assert asyncio.run(manager.upsert_collection("documents")) is True
    _, payload, _ = manager._http.put_calls[-1]
    assert payload["vectors"] == {"size": 768, "distance": "Dot"}
    assert payload["hnsw_config"] == {"m": 32, "ef_construct": 256}


def test_costing_distinguishes_priced_free_and_unpriced_models(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "AI_GATEWAY_CONFIG_PATH", _write_config(tmp_path, _base_config())
    )

    from aigateway_core.route.metrics.costing import (
        _estimate_cost,
        estimate_model_cost,
    )

    priced = estimate_model_cost("demo-model", 100, 50)
    assert priced.amount_usd == 0.4
    assert priced.status == "priced"

    free = estimate_model_cost("free-model", 100, 50)
    assert free.amount_usd == 0.0
    assert free.status == "free"

    unpriced = estimate_model_cost("gpt-4o", 100, 50)
    assert unpriced.amount_usd is None
    assert unpriced.status == "unpriced"

    assert _estimate_cost("provider/demo-model", 100) == 0.2
    unknown_numeric = _estimate_cost("gpt-4o", 100)
    assert unknown_numeric == 0.0
    assert unknown_numeric.pricing_status == "unpriced"
    assert unknown_numeric.pricing_known is False


def test_bridge_tracks_split_token_cost_and_pricing_status(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "AI_GATEWAY_CONFIG_PATH", _write_config(tmp_path, _base_config())
    )

    from aigateway_core.route.bridge import LiteLLMBridge

    bridge = LiteLLMBridge({})
    response = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        }
    }
    assert bridge._track_usage("demo-model", response) == 0.4
    assert response["_meta"]["pricing_status"] == "priced"

    unknown_response = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        }
    }
    tracked_unknown = bridge._track_usage("gpt-4o", unknown_response)
    assert tracked_unknown == 0.0
    assert tracked_unknown.pricing_status == "unpriced"
    assert unknown_response["_meta"]["pricing_status"] == "unpriced"
    assert bridge._estimate_cost("demo-model", 100) == 0.2
    estimated_unknown = bridge._estimate_cost("gpt-4o", 100)
    assert estimated_unknown == 0.0
    assert estimated_unknown.pricing_status == "unpriced"


def test_comfyui_missing_config_reports_configuration_error():
    from aigateway_api.local_generation import builtin_presets, probe_comfyui

    status = asyncio.run(probe_comfyui({}))
    assert status["available"] is False
    assert status["public_url"] == ""
    assert status["manager_url"] == ""
    assert status["configuration_status"] == "configuration_error"
    assert status["error"] == "config_missing:server_url"
    assert "config_missing:checkpoint_name" in status["configuration_errors"]

    presets = builtin_presets({})
    sdxl = next(item for item in presets if item["id"] == "sdxl-draft")
    assert sdxl["configuration_status"] == "configuration_error"
    assert sdxl["dependencies"]["models"] == []
    assert sdxl["configuration_errors"] == [
        "config_missing:checkpoint_name"
    ]

    serialized = json.dumps(presets, ensure_ascii=False)
    assert "http://localhost:8188" not in serialized
    assert "sd_xl_base_1.0.safetensors" not in serialized
    assert "RealESRGAN_x4plus.pth" not in serialized
    assert "checkpoints/\"" not in serialized


def test_draft_storage_must_be_configured(tmp_path):
    from aigateway_core.pipelines.generation._common.config import (
        DraftWorkflowConfig,
    )
    from aigateway_core.pipelines.generation._common.exceptions import (
        DraftWorkflowError,
    )
    from aigateway_core.pipelines.generation.draft.draft_cleaner import (
        DraftSessionCleaner,
    )
    from aigateway_core.pipelines.generation.draft.draft_generator import (
        DraftGeneratorStrategy,
    )

    unconfigured = DraftWorkflowConfig()
    assert unconfigured.store_dir == ""

    with pytest.raises(
        DraftWorkflowError,
        match="config_missing:generation_optimization.draft_workflow.store_dir",
    ):
        DraftGeneratorStrategy(unconfigured)

    with pytest.raises(ValueError, match="config_missing"):
        DraftSessionCleaner("", session_ttl_hours=24)

    configured_dir = str(tmp_path / "drafts")
    configured = DraftWorkflowConfig(store_dir=configured_dir)
    strategy = DraftGeneratorStrategy(configured)
    cleaner = DraftSessionCleaner(configured_dir, session_ttl_hours=24)
    assert strategy._store_dir == configured_dir
    assert cleaner._store_dir == configured_dir


def test_cors_preload_reads_yaml_without_overriding_explicit_env(
    tmp_path, monkeypatch
):
    config_path = _write_config(
        tmp_path,
        {
            "server": {
                "cors_origins": [
                    "https://panel.example",
                    "https://ops.example",
                ]
            }
        },
    )
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", config_path)
    monkeypatch.delenv("AI_GATEWAY_CORS_ORIGINS", raising=False)

    import aigateway_api

    aigateway_api._preload_cors_origins()
    assert (
        os.environ["AI_GATEWAY_CORS_ORIGINS"]
        == "https://panel.example,https://ops.example"
    )

    monkeypatch.setenv(
        "AI_GATEWAY_CORS_ORIGINS", "https://explicit.example"
    )
    aigateway_api._preload_cors_origins()
    assert (
        os.environ["AI_GATEWAY_CORS_ORIGINS"]
        == "https://explicit.example"
    )


def test_cors_preload_does_not_import_unrelated_dotenv_values(
    tmp_path, monkeypatch
):
    pytest.importorskip("dotenv")
    (tmp_path / ".env").write_text(
        "AI_GATEWAY_CORS_ORIGINS=https://dotenv.example\n"
        "AI_GATEWAY_ADMIN_USERNAME=dotenv-admin\n"
        "AI_GATEWAY_PASSWORD_PBKDF2_ITERATIONS=1\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AI_GATEWAY_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("AI_GATEWAY_ADMIN_USERNAME", raising=False)
    monkeypatch.delenv(
        "AI_GATEWAY_PASSWORD_PBKDF2_ITERATIONS", raising=False
    )

    import aigateway_api

    aigateway_api._preload_cors_origins()

    assert os.environ["AI_GATEWAY_CORS_ORIGINS"] == "https://dotenv.example"
    assert "AI_GATEWAY_ADMIN_USERNAME" not in os.environ
    assert "AI_GATEWAY_PASSWORD_PBKDF2_ITERATIONS" not in os.environ


def test_config_template_contains_required_runtime_values():
    repository_root = Path(__file__).resolve().parents[2]
    template = yaml.safe_load(
        (repository_root / "config.yaml.template").read_text(encoding="utf-8")
    )

    auth = template["auth"]
    assert auth["database_path"]
    assert auth["database_timeout_seconds"] > 0
    assert auth["admin_username"]
    assert auth["admin_user_id"]
    assert auth["session"]["idle_ttl_seconds"] > 0
    assert auth["session"]["absolute_ttl_seconds"] > 0
    assert auth["password"]["pbkdf2_iterations"] > 0

    redis = template["infrastructure"]["redis"]
    qdrant = template["infrastructure"]["qdrant"]
    assert redis["namespace"]
    assert qdrant["distance"]
    assert template["cache"]["key_buckets"]["max_tokens"]

    generation = template["generation_optimization"]
    assert generation["preset_store_dir"]
    assert generation["draft_workflow"]["store_dir"]
    assert generation["draft_workflow"]["retention_period_hours"] > 0
