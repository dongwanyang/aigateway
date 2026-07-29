"""Regression tests for config-backed runtime values."""

from __future__ import annotations

import asyncio
import json
import os

import pytest
import yaml


def _write_config(tmp_path, data: dict) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return str(path)


def _base_config() -> dict:
    return {
        "observability": {"otel_service_name": "test-gateway"},
        "infrastructure": {
            "redis": {
                "namespace": "tenant-a",
                "key_prefixes": {"media": "custom:media:"},
            }
        },
        "cache": {"pipeline_version": "9"},
        "media_optimization": {"media_cache_ttl": 123},
        "providers": {
            "demo": {
                "model_grouper": [
                    {
                        "models": [{"name": "demo-model"}],
                        "pricing": {
                            "demo-model": {"prompt": 0.002, "completion": 0.004}
                        },
                    }
                ]
            }
        },
    }


def test_runtime_values_use_yaml_namespace_and_explicit_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", _write_config(tmp_path, _base_config()))

    from aigateway_core.shared.runtime_values import (
        media_cache_ttl_seconds,
        redis_key_prefix,
    )

    assert redis_key_prefix("media") == "custom:media"
    assert redis_key_prefix("l2_index") == "tenant-a:l2:idx:v9"
    assert redis_key_prefix("l2_hash") == "tenant-a:cache:v9search"
    assert redis_key_prefix("prompt_template") == "tenant-a:prompt_template"
    assert media_cache_ttl_seconds() == 123


def test_media_cache_manager_uses_configured_prefix_and_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", _write_config(tmp_path, _base_config()))

    from aigateway_core.prefix.media.cache import MediaCacheManager

    class FakeRedis:
        redis = None

    manager = MediaCacheManager(FakeRedis())
    assert manager._key_prefix == "custom:media"
    assert manager._default_ttl == 123


def test_l2_operations_refresh_prefixes_lazily(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", _write_config(tmp_path, _base_config()))

    from aigateway_core.prefix.cache import l2_search

    result = asyncio.run(l2_search.ensure_index(None))
    assert result is False
    assert l2_search.L2_INDEX_NAME == "tenant-a:l2:idx:v9"
    assert l2_search.L2_HASH_PREFIX == "tenant-a:cache:v9search:"


def test_prompt_template_manager_resolves_instance_prefixes(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", _write_config(tmp_path, _base_config()))

    from aigateway_core.pipelines.generation._common.config import PromptTemplateConfig
    from aigateway_core.pipelines.generation.token import PromptTemplateManager

    manager = PromptTemplateManager(None, PromptTemplateConfig())
    assert manager.KEY_PREFIX == "tenant-a:prompt_template"
    assert manager.INDEX_PREFIX == "tenant-a:prompt_template_index"


def test_costing_uses_provider_pricing_and_no_builtin_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", _write_config(tmp_path, _base_config()))

    from aigateway_core.route.metrics.costing import _estimate_cost

    assert _estimate_cost("provider/demo-model", 100) == 0.2
    assert _estimate_cost("gpt-4o", 100) == 0.0


def test_bridge_uses_shared_config_backed_estimator(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", _write_config(tmp_path, _base_config()))

    from aigateway_core.route.bridge import LiteLLMBridge

    bridge = LiteLLMBridge({})
    assert bridge._estimate_cost("demo-model", 100) == 0.2
    assert bridge._estimate_cost("gpt-4o", 100) == 0.0


def test_comfyui_missing_config_does_not_assume_localhost():
    from aigateway_api.local_generation import builtin_presets, probe_comfyui

    status = asyncio.run(probe_comfyui({}))
    assert status["available"] is False
    assert status["public_url"] == ""
    assert status["manager_url"] == ""
    assert status["error"] == "config_missing:server_url"

    serialized = json.dumps(builtin_presets({}), ensure_ascii=False)
    assert "http://localhost:8188" not in serialized
    assert "sd_xl_base_1.0.safetensors" not in serialized
    assert "RealESRGAN_x4plus.pth" not in serialized


def test_draft_storage_must_be_configured(tmp_path):
    from aigateway_core.pipelines.generation._common.config import DraftWorkflowConfig
    from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
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


def test_cors_preload_reads_yaml_without_overriding_explicit_env(tmp_path, monkeypatch):
    config_path = _write_config(
        tmp_path,
        {"server": {"cors_origins": ["https://panel.example", "https://ops.example"]}},
    )
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", config_path)
    monkeypatch.delenv("AI_GATEWAY_CORS_ORIGINS", raising=False)

    import aigateway_api

    aigateway_api._preload_cors_origins()
    assert (
        os.environ["AI_GATEWAY_CORS_ORIGINS"]
        == "https://panel.example,https://ops.example"
    )

    monkeypatch.setenv("AI_GATEWAY_CORS_ORIGINS", "https://explicit.example")
    aigateway_api._preload_cors_origins()
    assert os.environ["AI_GATEWAY_CORS_ORIGINS"] == "https://explicit.example"
