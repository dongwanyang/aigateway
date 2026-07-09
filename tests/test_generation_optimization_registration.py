"""
test_generation_optimization_registration — 注册所有插件到 PipelineEngine 测试
=============================================================================

验证:
- 6 个优化插件正确注册到 PluginRegistry
- depends_on 依赖关系确保拓扑排序正确
- 根据配置启用/禁用各插件
- 禁用的策略注册但 enabled=False
- 全局 enabled=False 时所有插件禁用

需求: 6.1, 6.6
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

import pytest

from aigateway_core.shared.plugin_registry import PluginRegistry
from aigateway_core.pipelines.generation.registration import (
    AIDirectorPlugin,
    CostTrackerPlugin,
    DraftGeneratorPlugin,
    GenModelRouterPlugin,
    IntentEvaluatorPlugin,
    TokenCompressorPlugin,
    register_generation_optimization_plugins,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakePromptCachePlugin:
    """Fake prompt_cache plugin for dependency satisfaction."""

    name = "prompt_cache"
    enabled = True
    depends_on: list = []

    async def execute(self, ctx):
        return ctx


class _MockConfigManager:
    """Mock ConfigManager that returns config from a dict."""

    def __init__(self, config: dict):
        self._config = config

    def get(self, key, default=None):
        return self._config.get(key, default)


def _create_registry_with_prompt_cache() -> PluginRegistry:
    """Create a PluginRegistry with a fake prompt_cache plugin pre-registered."""
    registry = PluginRegistry()
    registry.register(
        name="prompt_cache",
        plugin_class=_FakePromptCachePlugin,
        enabled=True,
        depends_on=[],
        priority=0,
    )
    return registry


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPluginRegistration:
    """测试 6 个优化插件注册到 PluginRegistry."""

    def test_all_six_plugins_registered(self):
        """所有 6 个优化插件都被注册。"""
        registry = _create_registry_with_prompt_cache()
        register_generation_optimization_plugins(registry=registry)

        summary = registry.summary()
        expected_plugins = [
            "ai_director",
            "intent_evaluator",
            "token_compressor",
            "draft_generator",
            "gen_model_router",
            "cost_tracker",
        ]
        for name in expected_plugins:
            assert name in summary["plugins"], f"Plugin '{name}' not registered"

    def test_correct_depends_on_chain(self):
        """依赖链正确: ai_director→intent_evaluator→token_compressor→draft_generator→gen_model_router→cost_tracker."""
        registry = _create_registry_with_prompt_cache()
        register_generation_optimization_plugins(registry=registry)

        summary = registry.summary()
        plugins = summary["plugins"]

        assert plugins["ai_director"]["depends_on"] == ["prompt_cache"]
        assert plugins["intent_evaluator"]["depends_on"] == ["ai_director"]
        assert plugins["token_compressor"]["depends_on"] == ["intent_evaluator"]
        assert plugins["draft_generator"]["depends_on"] == ["token_compressor"]
        assert plugins["gen_model_router"]["depends_on"] == ["draft_generator"]
        assert plugins["cost_tracker"]["depends_on"] == ["gen_model_router"]

    def test_dependency_validation_passes(self):
        """依赖校验通过（无循环、无缺失依赖）。"""
        registry = _create_registry_with_prompt_cache()
        register_generation_optimization_plugins(registry=registry)

        errors = registry.validate_dependencies()
        assert errors == [], f"Dependency validation failed: {errors}"

    def test_topological_sort_order(self):
        """拓扑排序后的顺序正确。"""
        registry = _create_registry_with_prompt_cache()
        register_generation_optimization_plugins(registry=registry)

        all_plugins = registry.get_all()
        names = [p.name for p in all_plugins]

        # 验证依赖顺序：每个插件必须在其 depends_on 之后
        expected_order = [
            "prompt_cache",
            "ai_director",
            "intent_evaluator",
            "token_compressor",
            "draft_generator",
            "gen_model_router",
            "cost_tracker",
        ]
        for i, name in enumerate(expected_order):
            assert name in names, f"Plugin '{name}' not in ordered list"
            idx = names.index(name)
            # 验证当前插件在其依赖之后
            if i > 0:
                dep_idx = names.index(expected_order[i - 1])
                assert idx > dep_idx, (
                    f"Plugin '{name}' (idx={idx}) should come after "
                    f"'{expected_order[i-1]}' (idx={dep_idx})"
                )


class TestPluginEnableDisable:
    """测试根据配置启用/禁用各插件。"""

    def test_all_enabled_by_default(self):
        """默认配置下所有插件启用。"""
        registry = _create_registry_with_prompt_cache()
        register_generation_optimization_plugins(registry=registry)

        summary = registry.summary()
        for name in ["ai_director", "intent_evaluator", "token_compressor",
                     "draft_generator", "gen_model_router", "cost_tracker"]:
            assert summary["plugins"][name]["enabled"] is True

    def test_global_disabled(self):
        """generation_optimization.enabled=False 时所有插件禁用。"""
        config = {
            "generation_optimization": {"enabled": False},
            "providers": {},
            "auth": {},
        }
        registry = _create_registry_with_prompt_cache()
        register_generation_optimization_plugins(
            registry=registry,
            config_manager=_MockConfigManager(config),
        )

        summary = registry.summary()
        for name in ["ai_director", "intent_evaluator", "token_compressor",
                     "draft_generator", "gen_model_router", "cost_tracker"]:
            assert summary["plugins"][name]["enabled"] is False, (
                f"Plugin '{name}' should be disabled when global enabled=False"
            )

    def test_individual_strategy_disabled(self):
        """单独禁用某个策略时对应插件禁用。"""
        config = {
            "generation_optimization": {
                "enabled": True,
                "ai_director": {"enabled": False},
                "model_router": {"enabled": True},
                "token_compressor": {"enabled": True},
                "draft_workflow": {"enabled": True},
                "cost_tracking": {"enabled": True},
            },
            "providers": {},
            "auth": {},
        }
        registry = _create_registry_with_prompt_cache()
        register_generation_optimization_plugins(
            registry=registry,
            config_manager=_MockConfigManager(config),
        )

        summary = registry.summary()
        assert summary["plugins"]["ai_director"]["enabled"] is False
        assert summary["plugins"]["intent_evaluator"]["enabled"] is True
        assert summary["plugins"]["token_compressor"]["enabled"] is True

    def test_model_router_disabled_affects_intent_and_gen_router(self):
        """model_router.enabled=False 同时禁用 intent_evaluator 和 gen_model_router。"""
        config = {
            "generation_optimization": {
                "enabled": True,
                "ai_director": {"enabled": True},
                "model_router": {"enabled": False},
                "token_compressor": {"enabled": True},
                "draft_workflow": {"enabled": True},
                "cost_tracking": {"enabled": True},
            },
            "providers": {},
            "auth": {},
        }
        registry = _create_registry_with_prompt_cache()
        register_generation_optimization_plugins(
            registry=registry,
            config_manager=_MockConfigManager(config),
        )

        summary = registry.summary()
        assert summary["plugins"]["ai_director"]["enabled"] is True
        assert summary["plugins"]["intent_evaluator"]["enabled"] is False
        assert summary["plugins"]["gen_model_router"]["enabled"] is False
        # token_compressor and others still enabled
        assert summary["plugins"]["token_compressor"]["enabled"] is True
        assert summary["plugins"]["draft_generator"]["enabled"] is True
        assert summary["plugins"]["cost_tracker"]["enabled"] is True


class TestPluginInstantiation:
    """测试插件通过 PluginRegistry.get_all() 可正确实例化。"""

    def test_all_plugins_instantiate(self):
        """所有插件可通过 registry.get_all() 实例化。"""
        registry = _create_registry_with_prompt_cache()
        register_generation_optimization_plugins(registry=registry)

        all_plugins = registry.get_all()
        # Should have 7 plugins (1 fake + 6 gen opt)
        assert len(all_plugins) == 7

        # All should have execute method
        for plugin in all_plugins:
            assert hasattr(plugin, "execute"), f"Plugin '{plugin.name}' missing execute()"

    def test_plugin_classes_correct(self):
        """注册的插件类型正确。"""
        registry = _create_registry_with_prompt_cache()
        register_generation_optimization_plugins(registry=registry)

        all_plugins = registry.get_all()
        plugin_map = {p.name: p for p in all_plugins}

        assert isinstance(plugin_map["ai_director"], AIDirectorPlugin)
        assert isinstance(plugin_map["intent_evaluator"], IntentEvaluatorPlugin)
        assert isinstance(plugin_map["token_compressor"], TokenCompressorPlugin)
        assert isinstance(plugin_map["draft_generator"], DraftGeneratorPlugin)
        assert isinstance(plugin_map["gen_model_router"], GenModelRouterPlugin)
        assert isinstance(plugin_map["cost_tracker"], CostTrackerPlugin)


class TestDisabledPluginPassthrough:
    """测试禁用的插件透传请求到下一阶段。"""

    @pytest.mark.asyncio
    async def test_disabled_ai_director_passthrough(self):
        """禁用的 ai_director 插件透传上下文不做修改。"""
        config = {
            "generation_optimization": {
                "enabled": True,
                "ai_director": {"enabled": False},
            },
            "providers": {},
            "auth": {},
        }
        registry = _create_registry_with_prompt_cache()
        register_generation_optimization_plugins(
            registry=registry,
            config_manager=_MockConfigManager(config),
        )

        all_plugins = registry.get_all()
        plugin_map = {p.name: p for p in all_plugins}
        ai_director = plugin_map["ai_director"]

        # Create a mock PipelineContext
        from aigateway_core.dispatch.context import PipelineContext

        ctx = PipelineContext(request={"messages": [{"role": "user", "content": "hello"}]}, trace_id="test-trace")
        original_extra = dict(ctx.extra)

        # Execute disabled plugin — should pass through
        result_ctx = await ai_director.execute(ctx)
        # generation_optimization key should not be added with real data
        assert result_ctx is ctx

    @pytest.mark.asyncio
    async def test_disabled_token_compressor_passthrough(self):
        """禁用的 token_compressor 插件透传上下文不做修改。"""
        config = {
            "generation_optimization": {
                "enabled": True,
                "token_compressor": {"enabled": False},
            },
            "providers": {},
            "auth": {},
        }
        registry = _create_registry_with_prompt_cache()
        register_generation_optimization_plugins(
            registry=registry,
            config_manager=_MockConfigManager(config),
        )

        all_plugins = registry.get_all()
        plugin_map = {p.name: p for p in all_plugins}
        token_compressor = plugin_map["token_compressor"]

        from aigateway_core.dispatch.context import PipelineContext

        ctx = PipelineContext(request={"messages": [{"role": "user", "content": "hello"}]}, trace_id="test-trace")

        result_ctx = await token_compressor.execute(ctx)
        assert result_ctx is ctx


class TestDuplicateRegistration:
    """测试重复注册时的行为。"""

    def test_no_error_on_already_registered(self):
        """如果插件名已存在，跳过而不报错。"""
        registry = _create_registry_with_prompt_cache()

        # Register once
        register_generation_optimization_plugins(registry=registry)

        # Register again — should not raise
        # Since plugins are already registered, the function skips them
        register_generation_optimization_plugins(registry=registry)

        # Still 7 plugins
        summary = registry.summary()
        assert summary["total"] == 7
