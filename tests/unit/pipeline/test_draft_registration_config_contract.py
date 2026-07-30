"""Regression coverage for Draft plugin registration without deployment config."""
from __future__ import annotations

import asyncio

import pytest

from aigateway_core.pipelines.generation._common.config import DraftWorkflowConfig
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)
from aigateway_core.pipelines.generation.registration import (
    register_generation_optimization_plugins,
)
from aigateway_core.shared.plugin_registry import PluginRegistry


def test_registration_without_config_manager_keeps_complete_plugin_chain() -> None:
    registry = PluginRegistry()

    register_generation_optimization_plugins(registry=registry)

    summary = registry.summary()
    expected = {
        "ai_director",
        "intent_evaluator",
        "token_compressor",
        "draft_generator",
        "gen_model_router",
        "cost_tracker",
    }
    assert expected.issubset(summary["plugins"])
    assert registry.validate_dependencies() == []


def test_explicit_empty_store_dir_creates_unavailable_strategy() -> None:
    config = DraftWorkflowConfig()

    strategy = DraftGeneratorStrategy(config, store_dir="")

    assert strategy._store_dir == ""
    with pytest.raises(
        DraftWorkflowError,
        match="config_missing:generation_optimization.draft_workflow.store_dir",
    ):
        asyncio.run(strategy.check_local_dependencies(None))
