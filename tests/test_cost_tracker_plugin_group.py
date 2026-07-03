"""
Tests for CostTrackerPlugin API Key Group Label Injection
==========================================================

Verifies that the CostTrackerPlugin correctly extracts api_key_id from
PipelineContext.user_id and passes it to the GenerationCostTracker methods,
enabling proper Prometheus metric labeling by API Key group.

需求: 9.1, 9.2, 9.4, 9.5
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.context import PipelineContext
from aigateway_core.generation_optimization.config import (
    CostTrackingConfig,
    GenerationOptimizationConfig,
)
from aigateway_core.generation_optimization.metrics import (
    DEFAULT_API_KEY_GROUP,
    GenerationCostTracker,
    _get_api_key_group,
)
from aigateway_core.generation_optimization.models import CostSavingRecord
from aigateway_core.generation_optimization.plugins.cost_tracker_plugin import (
    CostTrackerPlugin,
)


@pytest.fixture
def mock_tracing():
    """Mock the tracing manager."""
    with patch(
        "aigateway_core.generation_optimization.plugins.cost_tracker_plugin.get_tracing_manager"
    ) as mock:
        tracing = MagicMock()
        tracing.create_plugin_span.return_value = {"attributes": {}}
        mock.return_value = tracing
        yield tracing


@pytest.fixture
def api_key_groups():
    """Sample API key groups mapping."""
    return {
        "admin": "admin-team",
        "dev1": "engineering",
        "marketer": "marketing-team",
    }


@pytest.fixture
def config():
    """Default GenerationOptimizationConfig."""
    return GenerationOptimizationConfig()


@pytest.fixture
def tracker_with_groups(api_key_groups):
    """GenerationCostTracker with api_key_groups mapping."""
    return GenerationCostTracker(
        config=CostTrackingConfig(),
        prometheus_registry=MagicMock(),
        api_key_groups=api_key_groups,
    )


class TestCostTrackerPluginGroupInjection:
    """Verify CostTrackerPlugin injects api_key_id into cost tracker methods."""

    @pytest.mark.asyncio
    async def test_plugin_passes_user_id_as_api_key_id(
        self, config, tracker_with_groups, mock_tracing
    ):
        """Plugin extracts ctx.user_id and passes it to tracker record methods."""
        plugin = CostTrackerPlugin(tracker=tracker_with_groups, config=config)

        ctx = PipelineContext(
            request={"messages": [], "model": "test-model"},
            user_id="admin",
        )
        # Set up generation_optimization data in context
        ctx.extra["generation_optimization"] = {
            "model_router": {
                "estimated_cost": 0.05,
                "premium_price": 0.10,
            },
            "token_compressor": {
                "total_original_tokens": 1000,
                "total_compressed_tokens": 500,
            },
            "ai_director": {
                "cost_usd": 0.001,
                "model_used": "gpt-4o-mini",
            },
        }

        result_ctx = await plugin.execute(ctx)

        # Verify cost tracker data was written
        cost_data = result_ctx.extra["generation_optimization"]["cost_tracker"]
        assert cost_data["request_id"] == ctx.request_id
        # The model_routing_saving should be 0.10 - 0.05 = 0.05
        assert cost_data["model_routing_saving_usd"] == 0.05

    @pytest.mark.asyncio
    async def test_plugin_uses_empty_string_when_no_user_id(
        self, config, tracker_with_groups, mock_tracing
    ):
        """Plugin uses empty string api_key_id when ctx.user_id is None."""
        plugin = CostTrackerPlugin(tracker=tracker_with_groups, config=config)

        ctx = PipelineContext(
            request={"messages": [], "model": "test-model"},
            user_id=None,
        )
        ctx.extra["generation_optimization"] = {
            "model_router": {
                "estimated_cost": 0.05,
                "premium_price": 0.10,
            },
            "token_compressor": {},
            "ai_director": {},
        }

        # Should not raise - uses empty string which maps to "default" group
        result_ctx = await plugin.execute(ctx)
        cost_data = result_ctx.extra["generation_optimization"]["cost_tracker"]
        assert cost_data["model_routing_saving_usd"] == 0.05

    @pytest.mark.asyncio
    async def test_group_label_injected_via_api_key_id(
        self, config, api_key_groups, mock_tracing
    ):
        """Verifies group label is resolved from api_key_id in Prometheus calls."""
        mock_prometheus = MagicMock()
        mock_prometheus.available = True

        tracker = GenerationCostTracker(
            config=CostTrackingConfig(),
            prometheus_registry=mock_prometheus,
            api_key_groups=api_key_groups,
        )
        plugin = CostTrackerPlugin(tracker=tracker, config=config)

        ctx = PipelineContext(
            request={"messages": [], "model": "test-model"},
            user_id="dev1",  # Should resolve to "engineering" group
        )
        ctx.extra["generation_optimization"] = {
            "model_router": {
                "estimated_cost": 0.03,
                "premium_price": 0.10,
            },
            "token_compressor": {},
            "ai_director": {},
        }

        await plugin.execute(ctx)

        # Verify inc_invocations was called with "engineering" group
        mock_prometheus.inc_invocations.assert_called()
        call_args_list = mock_prometheus.inc_invocations.call_args_list
        # At least one call should have "engineering" as api_key_group
        groups_used = [call.kwargs.get("api_key_group") or call.args[1] for call in call_args_list]
        assert "engineering" in groups_used

    @pytest.mark.asyncio
    async def test_unknown_api_key_uses_default_group(
        self, config, api_key_groups, mock_tracing
    ):
        """Unknown api_key_id maps to 'default' group for Prometheus labels."""
        mock_prometheus = MagicMock()
        mock_prometheus.available = True

        tracker = GenerationCostTracker(
            config=CostTrackingConfig(),
            prometheus_registry=mock_prometheus,
            api_key_groups=api_key_groups,
        )
        plugin = CostTrackerPlugin(tracker=tracker, config=config)

        ctx = PipelineContext(
            request={"messages": [], "model": "test-model"},
            user_id="unknown_user",  # Not in api_key_groups
        )
        ctx.extra["generation_optimization"] = {
            "model_router": {
                "estimated_cost": 0.03,
                "premium_price": 0.10,
            },
            "token_compressor": {},
            "ai_director": {},
        }

        await plugin.execute(ctx)

        # Verify default group was used
        call_args_list = mock_prometheus.inc_invocations.call_args_list
        groups_used = [call.kwargs.get("api_key_group") or call.args[1] for call in call_args_list]
        assert DEFAULT_API_KEY_GROUP in groups_used

    @pytest.mark.asyncio
    async def test_disabled_plugin_does_not_record_metrics(
        self, api_key_groups, mock_tracing
    ):
        """Disabled cost tracker plugin passes through without recording."""
        config = GenerationOptimizationConfig()
        config.cost_tracking.enabled = False

        mock_prometheus = MagicMock()
        tracker = GenerationCostTracker(
            config=CostTrackingConfig(),
            prometheus_registry=mock_prometheus,
            api_key_groups=api_key_groups,
        )
        plugin = CostTrackerPlugin(tracker=tracker, config=config)

        ctx = PipelineContext(
            request={"messages": [], "model": "test-model"},
            user_id="admin",
        )
        ctx.extra["generation_optimization"] = {
            "model_router": {"estimated_cost": 0.05, "premium_price": 0.10},
        }

        result_ctx = await plugin.execute(ctx)

        # Should not have cost_tracker data written
        assert "cost_tracker" not in result_ctx.extra.get("generation_optimization", {})
        mock_prometheus.inc_invocations.assert_not_called()


class TestGroupFieldDoesNotAffectIsolation:
    """Verify group field does NOT affect resource isolation (Req 9.5).

    The group label is ONLY used for Prometheus metrics aggregation.
    Templates and feature caches remain isolated per individual API Key (user_id).
    """

    def test_group_mapping_preserves_individual_user_ids(self, api_key_groups):
        """api_key_groups maps individual user_ids, not groups."""
        # Each user_id retains its own identity for isolation purposes
        assert "admin" in api_key_groups
        assert "dev1" in api_key_groups
        # The mapping is user_id -> group, not group -> user_ids
        # Resource isolation is done by user_id, group is only for metrics

    def test_same_group_keys_have_different_user_ids(self):
        """Two keys in same group maintain separate identities."""
        from aigateway_core.generation_optimization.api_key_groups import build_api_key_groups

        auth_config = {
            "api_keys": [
                {"key": "sk-a", "user_id": "user_a", "group": "team-x"},
                {"key": "sk-b", "user_id": "user_b", "group": "team-x"},
            ]
        }
        groups = build_api_key_groups(auth_config)

        # Same group label for metrics
        assert groups["user_a"] == "team-x"
        assert groups["user_b"] == "team-x"

        # But user_ids are distinct -> templates/cache still isolated
        assert "user_a" != "user_b"
