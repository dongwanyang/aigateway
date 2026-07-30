"""Contract tests for explicit config wrappers replacing import-time patches."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import yaml

from aigateway_core.pipelines.generation._common.config import (
    DraftWorkflowConfig,
    parse_generation_optimization_config,
)
from aigateway_core.shared.integration_configs import (
    ConvCompressorConfig,
    PromptCompressConfig,
    RAGRetrieverConfig,
)
from aigateway_core.shared.plugin_registry import PluginRegistry


def test_generation_config_resolves_store_dir_by_field_name() -> None:
    assert DraftWorkflowConfig().store_dir == ""
    assert parse_generation_optimization_config({}).draft_workflow.store_dir == ""

    configured = parse_generation_optimization_config(
        {"draft_workflow": {"store_dir": "/data/runtime-drafts"}}
    )
    assert configured.draft_workflow.store_dir == "/data/runtime-drafts"

    reloaded = parse_generation_optimization_config({}, previous=configured)
    assert reloaded.draft_workflow.store_dir == "/data/runtime-drafts"


def test_builtin_registration_injects_qdrant_without_mutating_environment(
    monkeypatch,
) -> None:
    from aigateway_core.prefix.registration import _register_builtin_plugins

    rag_config = RAGRetrieverConfig(
        collection_name="documents",
        embedding_model="test-embedding",
    )
    integration_configs = SimpleNamespace(
        prompt_compress=PromptCompressConfig(),
        rag_retriever=rag_config,
        conv_compressor=ConvCompressorConfig(),
    )

    class ConfigManagerStub:
        integration_configs = integration_configs

        @staticmethod
        def get(path, default=None):
            values = {
                "plugins": [
                    {"name": "rag_retriever", "enabled": True},
                    {"name": "conv_compressor", "enabled": False},
                ],
                "code_rag": {"graph_db_dir": "/data/code-graphs"},
                "infrastructure": {
                    "qdrant": {"url": "http://qdrant.internal:6333"}
                },
                "media_optimization": {"enabled": False},
                "generation_optimization": {"enabled": False},
            }
            return values.get(path, default)

    monkeypatch.delenv("AI_GATEWAY_QDRANT_URL", raising=False)
    registry = PluginRegistry()

    _register_builtin_plugins(registry, ConfigManagerStub())

    assert "AI_GATEWAY_QDRANT_URL" not in os.environ
    registration = registry._registrations["rag_retriever"]
    configured_rag = registration.config["config"]
    assert configured_rag.qdrant_url == "http://qdrant.internal:6333"
    assert configured_rag.code_graph_db_dir == "/data/code-graphs"


def test_l2_namespace_is_passed_without_mutating_implementation(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "infrastructure": {"redis": {"namespace": "isolated-gateway"}},
                "cache": {"pipeline_version": "12"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", str(config_path))

    from aigateway_core.prefix.cache import _l2_search_impl as implementation
    from aigateway_core.prefix.cache import l2_search

    implementation_defaults = (
        implementation.L2_INDEX_NAME,
        implementation.L2_HASH_PREFIX,
    )

    assert asyncio.run(l2_search.ensure_index(None)) is False
    assert l2_search.L2_INDEX_NAME == "isolated-gateway:l2:idx:v12"
    assert l2_search.L2_HASH_PREFIX == "isolated-gateway:cache:v12search:"
    assert (
        implementation.L2_INDEX_NAME,
        implementation.L2_HASH_PREFIX,
    ) == implementation_defaults


def test_package_initializers_do_not_assign_runtime_methods() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    initializers = [
        "aigateway-core/src/aigateway_core/shared/__init__.py",
        "aigateway-core/src/aigateway_core/shared/auth/__init__.py",
        "aigateway-core/src/aigateway_core/prefix/cache/__init__.py",
        "aigateway-core/src/aigateway_core/pipelines/generation/_common/__init__.py",
        "aigateway-core/src/aigateway_core/pipelines/generation/draft/__init__.py",
        "aigateway-core/src/aigateway_core/pipelines/generation/token/__init__.py",
        "aigateway-core/src/aigateway_core/pipelines/understanding/compression/__init__.py",
        "aigateway-core/src/aigateway_core/route/bridge/__init__.py",
    ]
    forbidden = (
        ".__init__ =",
        ".connect =",
        ".upsert_collection =",
        "._estimate_cost =",
        "._load_clip_model =",
        "._init_compressor =",
        ".ensure_index =",
        ".store =",
        ".search =",
    )

    for relative_path in initializers:
        source = (repository_root / relative_path).read_text(encoding="utf-8")
        assert not any(pattern in source for pattern in forbidden), relative_path
