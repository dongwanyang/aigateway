"""
Tests for ModelRouterStrategy — 智能模型路由核心逻辑
=====================================================

验证:
- 正常路由: 按模态筛选 → capability >= complexity → 选最低价格
- 无合格模型时选最高 capability
- routing_hint "best quality": 选最高 capability
- routing_hint "cheapest": 选最低价格
- routing_hint 具体模型名: 直接选择
- model_override 存在: 直接使用, reason="override"
- model_override 不存在: 抛出 ModelRoutingError
- 模型不可用时 fallback: 按 fallback_models 降级
- 跨 provider fallback
- _build_model_list 正确合并 providers_config 和 capabilities

需求: 2.2, 2.4, 2.5, 2.6, 2.9, 2.10
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.generation_optimization.config import ModelRouterConfig
from aigateway_core.generation_optimization.exceptions import ModelRoutingError
from aigateway_core.generation_optimization.models import RoutingDecision
from aigateway_core.generation_optimization.strategies.model_router import (
    ModelConfig,
    ModelRouterStrategy,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(
    capabilities=None, modalities=None, default_model="model-a"
) -> ModelRouterConfig:
    """创建测试用 ModelRouterConfig."""
    return ModelRouterConfig(
        enabled=True,
        default_model=default_model,
        evaluation_timeout_seconds=2.0,
        model_capabilities=capabilities or {},
        model_modalities=modalities or {},
    )


def _make_providers_config():
    """创建测试用 providers_config."""
    return {
        "provider-a": {
            "api_key": "sk-test",
            "base_url": "https://api.a.com",
            "model_grouper": [
                {
                    "models": ["model-a", "model-b", "model-c"],
                    "fallback_models": ["model-b", "model-c"],
                    "pricing": {
                        "model-a": {"prompt": 0.01, "completion": 0.5},
                        "model-b": {"prompt": 0.05, "completion": 1.0},
                        "model-c": {"prompt": 0.10, "completion": 2.0},
                    },
                }
            ],
        },
        "provider-b": {
            "api_key": "sk-test2",
            "base_url": "https://api.b.com",
            "model_grouper": [
                {
                    "models": ["model-d", "model-e"],
                    "fallback_models": ["model-e"],
                    "pricing": {
                        "model-d": {"prompt": 0.03, "completion": 0.8},
                        "model-e": {"prompt": 0.08, "completion": 1.5},
                    },
                }
            ],
        },
    }


def _make_strategy(
    capabilities=None, modalities=None, default_model="model-a", providers=None
):
    """创建测试用 ModelRouterStrategy."""
    config = _make_config(
        capabilities=capabilities or {
            "model-a": 30,
            "model-b": 60,
            "model-c": 90,
            "model-d": 50,
            "model-e": 80,
        },
        modalities=modalities or {
            "model-a": ["generative"],
            "model-b": ["generative"],
            "model-c": ["generative"],
            "model-d": ["generative"],
            "model-e": ["generative"],
        },
        default_model=default_model,
    )
    return ModelRouterStrategy(
        config=config, providers_config=providers or _make_providers_config()
    )


# ---------------------------------------------------------------------------
# Tests: _build_model_list
# ---------------------------------------------------------------------------


class TestBuildModelList:
    def test_builds_correct_model_count(self):
        strategy = _make_strategy()
        models = strategy.get_model_list()
        assert len(models) == 5

    def test_model_attributes(self):
        strategy = _make_strategy()
        models = strategy.get_model_list()
        model_a = next(m for m in models if m.name == "model-a")
        assert model_a.provider == "provider-a"
        assert model_a.modality == ["generative"]
        assert model_a.capability_score == 30
        assert model_a.price_per_request == 0.01
        assert model_a.fallback_models == ["model-b", "model-c"]
        assert model_a.is_available is True

    def test_default_capability_for_unknown_model(self):
        """未在 model_capabilities 中注册的模型默认 score=50."""
        config = _make_config(
            capabilities={"model-a": 70},  # 只有 model-a 有配置
            modalities={"model-a": ["generative"], "model-b": ["generative"]},
        )
        providers = {
            "p": {
                "model_grouper": [
                    {"models": ["model-a", "model-b"], "fallback_models": [], "pricing": {}}
                ]
            }
        }
        strategy = ModelRouterStrategy(config=config, providers_config=providers)
        models = strategy.get_model_list()
        model_b = next(m for m in models if m.name == "model-b")
        assert model_b.capability_score == 50  # default

    def test_empty_providers_config(self):
        config = _make_config()
        strategy = ModelRouterStrategy(config=config, providers_config={})
        assert strategy.get_model_list() == []


# ---------------------------------------------------------------------------
# Tests: Normal Routing
# ---------------------------------------------------------------------------


class TestNormalRouting:
    def test_selects_cheapest_qualifying_model(self):
        """capability >= complexity 的模型中选最便宜的."""
        strategy = _make_strategy()
        # complexity=50, qualified: model-b(cap=60,price=0.05), model-c(cap=90,price=0.10),
        #                          model-d(cap=50,price=0.03), model-e(cap=80,price=0.08)
        # cheapest qualified: model-d at 0.03
        decision = asyncio.run(
            strategy.route(complexity_score=50, required_modality="generative")
        )
        assert decision.selected_model == "model-d"
        assert decision.selected_provider == "provider-b"
        assert decision.reason == "complexity"
        assert decision.estimated_cost == 0.03

    def test_selects_highest_capability_when_none_qualify(self):
        """没有模型 capability >= complexity 时选最高 capability."""
        strategy = _make_strategy()
        # complexity=95 → no model has capability >= 95
        # highest capability is model-c at 90
        decision = asyncio.run(
            strategy.route(complexity_score=95, required_modality="generative")
        )
        assert decision.selected_model == "model-c"
        assert decision.reason == "complexity"

    def test_filters_by_modality(self):
        """只选择匹配 required_modality 的模型."""
        strategy = _make_strategy(
            modalities={
                "model-a": ["llm"],
                "model-b": ["generative"],
                "model-c": ["generative"],
                "model-d": ["mllm"],
                "model-e": ["mllm"],
            }
        )
        # required_modality="llm" → only model-a qualifies
        decision = asyncio.run(
            strategy.route(complexity_score=0, required_modality="llm")
        )
        assert decision.selected_model == "model-a"

    def test_no_modality_match_uses_default(self):
        """没有匹配模态的模型时使用默认模型."""
        strategy = _make_strategy(
            modalities={
                "model-a": ["generative"],
                "model-b": ["generative"],
                "model-c": ["generative"],
                "model-d": ["generative"],
                "model-e": ["generative"],
            }
        )
        decision = asyncio.run(
            strategy.route(complexity_score=50, required_modality="mllm")
        )
        assert decision.selected_model == "model-a"  # default_model
        assert decision.reason == "fallback"

    def test_modality_membership_multi(self):
        """modality 列表含多个元素时，任一元素与 required_modality 相符即命中."""
        strategy = _make_strategy(
            modalities={
                "model-a": ["llm", "mllm"],
                "model-b": ["generative"],
                "model-c": ["generative"],
                "model-d": ["generative"],
                "model-e": ["generative"],
            }
        )
        # required_modality=llm → model-a 命中
        d1 = asyncio.run(
            strategy.route(complexity_score=0, required_modality="llm")
        )
        assert d1.selected_model == "model-a"
        # required_modality=mllm → model-a 依旧命中
        d2 = asyncio.run(
            strategy.route(complexity_score=0, required_modality="mllm")
        )
        assert d2.selected_model == "model-a"
        # required_modality=generative → model-a 被过滤（因不含 generative）
        d3 = asyncio.run(
            strategy.route(complexity_score=0, required_modality="generative")
        )
        assert d3.selected_model != "model-a"


# ---------------------------------------------------------------------------
# Tests: model_override
# ---------------------------------------------------------------------------


class TestModelOverride:
    def test_override_selects_exact_model(self):
        strategy = _make_strategy()
        decision = asyncio.run(
            strategy.route(
                complexity_score=10,
                required_modality="generative",
                model_override="model-c",
            )
        )
        assert decision.selected_model == "model-c"
        assert decision.selected_provider == "provider-a"
        assert decision.reason == "override"
        assert decision.estimated_cost == 0.10

    def test_override_nonexistent_model_raises_error(self):
        strategy = _make_strategy()
        with pytest.raises(ModelRoutingError, match="不存在"):
            asyncio.run(
                strategy.route(
                    complexity_score=10,
                    required_modality="generative",
                    model_override="nonexistent-model",
                )
            )

    def test_override_unavailable_model_triggers_fallback(self):
        strategy = _make_strategy()
        strategy.update_model_availability("model-c", False)
        decision = asyncio.run(
            strategy.route(
                complexity_score=10,
                required_modality="generative",
                model_override="model-c",
            )
        )
        # fallback_models for model-c is ["model-b", "model-c"]
        # model-b is available
        assert decision.selected_model == "model-b"
        assert decision.reason == "fallback"


# ---------------------------------------------------------------------------
# Tests: routing_hint
# ---------------------------------------------------------------------------


class TestRoutingHint:
    def test_best_quality_selects_highest_capability(self):
        strategy = _make_strategy()
        decision = asyncio.run(
            strategy.route(
                complexity_score=10,
                required_modality="generative",
                routing_hint="best quality",
            )
        )
        # Highest capability in generative: model-c(90)
        assert decision.selected_model == "model-c"
        assert decision.reason == "hint"

    def test_cheapest_selects_lowest_price(self):
        strategy = _make_strategy()
        decision = asyncio.run(
            strategy.route(
                complexity_score=10,
                required_modality="generative",
                routing_hint="cheapest",
            )
        )
        # Lowest price in generative: model-a(0.01)
        assert decision.selected_model == "model-a"
        assert decision.reason == "hint"

    def test_hint_specific_model_name(self):
        strategy = _make_strategy()
        decision = asyncio.run(
            strategy.route(
                complexity_score=10,
                required_modality="generative",
                routing_hint="model-d",
            )
        )
        assert decision.selected_model == "model-d"
        assert decision.reason == "hint"

    def test_invalid_hint_falls_back_to_normal_routing(self):
        strategy = _make_strategy()
        decision = asyncio.run(
            strategy.route(
                complexity_score=50,
                required_modality="generative",
                routing_hint="nonexistent-hint",
            )
        )
        # Falls back to normal routing, cheapest with cap>=50
        assert decision.reason == "complexity"

    def test_best_quality_case_insensitive(self):
        strategy = _make_strategy()
        decision = asyncio.run(
            strategy.route(
                complexity_score=10,
                required_modality="generative",
                routing_hint="Best Quality",
            )
        )
        assert decision.selected_model == "model-c"
        assert decision.reason == "hint"


# ---------------------------------------------------------------------------
# Tests: Fallback
# ---------------------------------------------------------------------------


class TestFallback:
    def test_fallback_to_same_provider_models(self):
        """模型不可用时按 fallback_models 列表降级."""
        strategy = _make_strategy()
        strategy.update_model_availability("model-b", False)
        # model-b's fallback_models = ["model-b", "model-c"]
        # model-b itself is unavailable, so next is model-c
        decision = asyncio.run(
            strategy.route(
                complexity_score=55,
                required_modality="generative",
            )
        )
        # Normal routing: cap>=55 → model-b(60), model-c(90), model-e(80)
        # model-b unavailable, so qualified available: model-c(0.10), model-d(cap=50 <55), model-e(0.08)
        # cheapest qualified available: model-e at 0.08
        assert decision.selected_model == "model-e"
        assert decision.reason == "complexity"

    def test_cross_provider_fallback(self):
        """同 provider 全部不可用时跨 provider 降级."""
        strategy = _make_strategy()
        # Make all provider-a models unavailable
        strategy.update_model_availability("model-a", False)
        strategy.update_model_availability("model-b", False)
        strategy.update_model_availability("model-c", False)

        decision = asyncio.run(
            strategy.route(
                complexity_score=10,
                required_modality="generative",
            )
        )
        # Only provider-b models available: model-d, model-e
        # cap>=10: both qualify, cheapest is model-d(0.03)
        assert decision.selected_model == "model-d"
        assert decision.selected_provider == "provider-b"

    def test_all_unavailable_uses_default(self):
        """所有同模态模型不可用时使用默认模型."""
        strategy = _make_strategy()
        for m in strategy.get_model_list():
            strategy.update_model_availability(m.name, False)

        decision = asyncio.run(
            strategy.route(
                complexity_score=10,
                required_modality="generative",
            )
        )
        assert decision.selected_model == "model-a"
        assert decision.reason == "fallback"


# ---------------------------------------------------------------------------
# Tests: update_model_availability
# ---------------------------------------------------------------------------


class TestUpdateAvailability:
    def test_updates_model_availability(self):
        strategy = _make_strategy()
        strategy.update_model_availability("model-a", False)
        models = strategy.get_model_list()
        model_a = next(m for m in models if m.name == "model-a")
        assert model_a.is_available is False

    def test_updates_nonexistent_model_noop(self):
        """更新不存在的模型名称不会报错."""
        strategy = _make_strategy()
        strategy.update_model_availability("ghost-model", False)  # no error


# ---------------------------------------------------------------------------
# Tests: RoutingDecision dataclass fields
# ---------------------------------------------------------------------------


class TestRoutingDecisionMetadata:
    def test_decision_has_all_fields(self):
        strategy = _make_strategy()
        decision = asyncio.run(
            strategy.route(complexity_score=40, required_modality="generative")
        )
        assert isinstance(decision.selected_model, str)
        assert isinstance(decision.selected_provider, str)
        assert decision.reason in ("complexity", "hint", "override", "fallback")
        assert isinstance(decision.complexity_score, int)
        assert isinstance(decision.estimated_cost, float)
        assert decision.complexity_score == 40
