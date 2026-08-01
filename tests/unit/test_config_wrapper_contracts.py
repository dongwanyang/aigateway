"""Contract tests for explicit config wrappers replacing import-time patches."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import yaml
from aigateway_core.pipelines.generation._common.config import (
    DraftWorkflowConfig,
    parse_generation_optimization_config,
)
from aigateway_core.route.metrics.costing import CostEstimate, PricingCost
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

    manager = ConfigManagerStub()
    manager.integration_configs = integration_configs
    monkeypatch.delenv("AI_GATEWAY_QDRANT_URL", raising=False)
    registry = PluginRegistry()

    _register_builtin_plugins(registry, manager)

    assert "AI_GATEWAY_QDRANT_URL" not in os.environ
    registration = registry.get("rag_retriever")
    assert registration is not None
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


def test_quota_model_usage_distinguishes_free_and_unpriced() -> None:
    from aigateway_core.shared.auth.sqlite_store import SQLiteStore

    store = object.__new__(SQLiteStore)
    unpriced_cost = PricingCost(
        CostEstimate(
            amount_usd=None,
            status="unpriced",
            prompt_tokens=10,
            completion_tokens=5,
        )
    )
    free_cost = PricingCost(
        CostEstimate(
            amount_usd=0.0,
            status="free",
            prompt_tokens=8,
            completion_tokens=2,
        )
    )

    quota = store._accumulate_quota(
        None,
        tokens=15,
        cost=unpriced_cost,
        model="unknown-model",
        tokens_in=10,
        tokens_out=5,
    )
    quota = store._accumulate_quota(
        quota,
        tokens=10,
        cost=free_cost,
        model="free-model",
        tokens_in=8,
        tokens_out=2,
    )

    model_usage = json.loads(quota["model_usage"])
    assert model_usage["unknown-model"]["pricing_status"] == "unpriced"
    assert model_usage["unknown-model"]["unpriced_requests"] == 1
    assert model_usage["free-model"]["pricing_status"] == "free"
    assert model_usage["free-model"]["free_requests"] == 1


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
