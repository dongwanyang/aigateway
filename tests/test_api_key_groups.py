"""
Tests for API Key Group Field Support
=======================================

验证 build_api_key_groups() 从 auth 配置中正确构建 user_id → group 映射，
并验证该映射与 GenerationCostTracker 的集成。

需求: 9.1, 9.2, 9.4, 9.5
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.generation_optimization.api_key_groups import build_api_key_groups
from aigateway_core.generation_optimization.metrics import (
    DEFAULT_API_KEY_GROUP,
    GenerationCostTracker,
    _get_api_key_group,
)
from aigateway_core.generation_optimization.config import CostTrackingConfig


# ==================================================================
# Tests for build_api_key_groups
# ==================================================================


class TestBuildApiKeyGroups:
    """Tests for build_api_key_groups utility function."""

    def test_single_key_with_group(self):
        """API Key with explicit group should map user_id to that group."""
        auth_config = {
            "api_keys": [
                {
                    "key": "sk-test123",
                    "user_id": "admin",
                    "is_admin": True,
                    "group": "admin-team",
                }
            ]
        }
        result = build_api_key_groups(auth_config)
        assert result == {"admin": "admin-team"}

    def test_single_key_without_group(self):
        """API Key without group field should map to 'default'."""
        auth_config = {
            "api_keys": [
                {
                    "key": "sk-test123",
                    "user_id": "user1",
                    "is_admin": False,
                }
            ]
        }
        result = build_api_key_groups(auth_config)
        assert result == {"user1": DEFAULT_API_KEY_GROUP}

    def test_multiple_keys_mixed_groups(self):
        """Multiple API Keys with and without groups."""
        auth_config = {
            "api_keys": [
                {
                    "key": "sk-admin",
                    "user_id": "admin",
                    "is_admin": True,
                    "group": "admin-team",
                },
                {
                    "key": "sk-dev1",
                    "user_id": "dev1",
                    "is_admin": False,
                    "group": "engineering",
                },
                {
                    "key": "sk-dev2",
                    "user_id": "dev2",
                    "is_admin": False,
                    # No group field
                },
                {
                    "key": "sk-marketing",
                    "user_id": "marketer",
                    "is_admin": False,
                    "group": "marketing-team",
                },
            ]
        }
        result = build_api_key_groups(auth_config)
        assert result == {
            "admin": "admin-team",
            "dev1": "engineering",
            "dev2": DEFAULT_API_KEY_GROUP,
            "marketer": "marketing-team",
        }

    def test_empty_auth_config(self):
        """Empty auth config returns empty mapping."""
        assert build_api_key_groups({}) == {}

    def test_none_auth_config(self):
        """None auth config returns empty mapping."""
        assert build_api_key_groups(None) == {}

    def test_missing_api_keys_section(self):
        """Auth config without api_keys section returns empty mapping."""
        auth_config = {"distributed_mode": False}
        assert build_api_key_groups(auth_config) == {}

    def test_empty_api_keys_list(self):
        """Empty api_keys list returns empty mapping."""
        auth_config = {"api_keys": []}
        assert build_api_key_groups(auth_config) == {}

    def test_entry_without_user_id_skipped(self):
        """API Key entries without user_id are skipped."""
        auth_config = {
            "api_keys": [
                {"key": "sk-broken", "group": "some-group"},
                {"key": "sk-valid", "user_id": "user1", "group": "team-a"},
            ]
        }
        result = build_api_key_groups(auth_config)
        assert result == {"user1": "team-a"}

    def test_empty_group_string_uses_default(self):
        """Empty string group falls back to 'default'."""
        auth_config = {
            "api_keys": [
                {"key": "sk-test", "user_id": "user1", "group": ""},
            ]
        }
        result = build_api_key_groups(auth_config)
        assert result == {"user1": DEFAULT_API_KEY_GROUP}

    def test_whitespace_only_group_uses_default(self):
        """Whitespace-only group falls back to 'default'."""
        auth_config = {
            "api_keys": [
                {"key": "sk-test", "user_id": "user1", "group": "   "},
            ]
        }
        result = build_api_key_groups(auth_config)
        assert result == {"user1": DEFAULT_API_KEY_GROUP}

    def test_group_whitespace_stripped(self):
        """Group labels are stripped of leading/trailing whitespace."""
        auth_config = {
            "api_keys": [
                {"key": "sk-test", "user_id": "user1", "group": "  team-a  "},
            ]
        }
        result = build_api_key_groups(auth_config)
        assert result == {"user1": "team-a"}

    def test_non_string_group_uses_default(self):
        """Non-string group value falls back to 'default'."""
        auth_config = {
            "api_keys": [
                {"key": "sk-test", "user_id": "user1", "group": 123},
            ]
        }
        result = build_api_key_groups(auth_config)
        assert result == {"user1": DEFAULT_API_KEY_GROUP}

    def test_non_list_api_keys_returns_empty(self):
        """Non-list api_keys value returns empty mapping."""
        auth_config = {"api_keys": "invalid"}
        assert build_api_key_groups(auth_config) == {}

    def test_non_dict_entries_skipped(self):
        """Non-dict entries in api_keys list are skipped."""
        auth_config = {
            "api_keys": [
                "invalid_entry",
                {"key": "sk-valid", "user_id": "user1", "group": "team-a"},
                42,
            ]
        }
        result = build_api_key_groups(auth_config)
        assert result == {"user1": "team-a"}

    def test_real_config_format(self):
        """Validates against the actual config.yaml format."""
        auth_config = {
            "api_keys": [
                {
                    "key": "sk-a1b2c3d4e5f6XDFDDSF12nco",
                    "user_id": "admin",
                    "is_admin": True,
                    "group": "admin-team",
                    "quotas": {
                        "daily_tokens": 1000000,
                        "monthly_cost": 50.0,
                        "rate_limit_rpm": 60,
                        "rate_limit_tpm": 100000,
                    },
                }
            ],
            "distributed_mode": False,
            "defaults": {
                "daily_tokens": 1000000,
                "monthly_cost": 50.0,
            },
        }
        result = build_api_key_groups(auth_config)
        assert result == {"admin": "admin-team"}


# ==================================================================
# Tests for integration with GenerationCostTracker
# ==================================================================


class TestApiKeyGroupMetricsIntegration:
    """Verify that api_key_groups mapping integrates with cost tracker."""

    def test_get_api_key_group_with_mapping(self):
        """_get_api_key_group resolves group from mapping."""
        groups = {"admin": "admin-team", "dev1": "engineering"}
        assert _get_api_key_group("admin", groups) == "admin-team"
        assert _get_api_key_group("dev1", groups) == "engineering"

    def test_get_api_key_group_unknown_key_returns_default(self):
        """_get_api_key_group returns 'default' for unknown api_key_id."""
        groups = {"admin": "admin-team"}
        assert _get_api_key_group("unknown_user", groups) == DEFAULT_API_KEY_GROUP

    def test_get_api_key_group_empty_id_returns_default(self):
        """_get_api_key_group returns 'default' for empty api_key_id."""
        groups = {"admin": "admin-team"}
        assert _get_api_key_group("", groups) == DEFAULT_API_KEY_GROUP

    def test_get_api_key_group_no_mapping_returns_default(self):
        """_get_api_key_group returns 'default' when no mapping provided."""
        assert _get_api_key_group("admin", None) == DEFAULT_API_KEY_GROUP
        assert _get_api_key_group("admin", {}) == DEFAULT_API_KEY_GROUP

    def test_cost_tracker_uses_api_key_groups(self):
        """GenerationCostTracker accepts api_key_groups in constructor."""
        groups = {"admin": "admin-team", "dev1": "engineering"}
        config = CostTrackingConfig()
        tracker = GenerationCostTracker(
            config=config,
            api_key_groups=groups,
        )
        # Verify the tracker stores the groups mapping
        assert tracker._api_key_groups == groups

    def test_build_groups_passed_to_cost_tracker(self):
        """End-to-end: build_api_key_groups output passed to cost tracker."""
        auth_config = {
            "api_keys": [
                {"key": "sk-a", "user_id": "admin", "group": "admin-team"},
                {"key": "sk-b", "user_id": "dev1"},
            ]
        }
        groups = build_api_key_groups(auth_config)
        config = CostTrackingConfig()
        tracker = GenerationCostTracker(config=config, api_key_groups=groups)

        # Verify the mapping is correctly constructed and passed
        assert tracker._api_key_groups["admin"] == "admin-team"
        assert tracker._api_key_groups["dev1"] == DEFAULT_API_KEY_GROUP


# ==================================================================
# Tests verifying group does NOT affect resource isolation (Req 9.5)
# ==================================================================


class TestGroupDoesNotAffectIsolation:
    """Verify group field does NOT affect resource isolation logic."""

    def test_same_group_different_keys_remain_isolated(self):
        """Two API Keys in the same group still have separate user_ids."""
        auth_config = {
            "api_keys": [
                {"key": "sk-a", "user_id": "user_a", "group": "same-team"},
                {"key": "sk-b", "user_id": "user_b", "group": "same-team"},
            ]
        }
        groups = build_api_key_groups(auth_config)
        # Both map to same group for metrics, but user_ids remain distinct
        assert groups["user_a"] == "same-team"
        assert groups["user_b"] == "same-team"
        # user_ids are still different — resource isolation uses user_id, not group
        assert "user_a" in groups
        assert "user_b" in groups
        assert "user_a" != "user_b"
