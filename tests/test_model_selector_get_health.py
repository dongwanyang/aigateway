"""Tests for ModelSelector.get_health() public method."""

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.route.model_resolution.model_selector import ModelSelector


def _make_bridge_with_cooldown(cooldown_status=None):
    """Create a bridge mock with a configurable cooldown tracker."""
    bridge = MagicMock()
    bridge._model_capabilities = {
        "agnes-2.0-flash": ["text", "image", "video"],
        "deepseek-v4-flash": ["text"],
    }
    bridge._model_pricing = {}
    bridge.get_registered_models = MagicMock(
        return_value=["agnes-2.0-flash", "deepseek-v4-flash"]
    )
    cooldown = MagicMock()
    cooldown.get_all_status = MagicMock(return_value=cooldown_status)
    bridge._cooldown_tracker = cooldown
    return bridge


class TestGetHealth:
    """Tests for ModelSelector.get_health()."""

    def test_get_health_known_model_healthy(self):
        """get_health returns healthy=True for CLOSED state model."""
        bridge = _make_bridge_with_cooldown({
            "deepseek-v4-flash": {
                "state": "CLOSED",
                "state_value": 0,
                "failure_count": 0,
                "last_failure_time": 0.0,
                "last_success_time": 0.0,
                "cooldown_until": None,
            },
        })
        sel = ModelSelector(bridge=bridge, config={})
        health = sel.get_health("deepseek-v4-flash")

        assert health["healthy"] is True
        assert health["failure_count"] == 0
        assert health["state"] == "CLOSED"

    def test_get_health_known_model_unhealthy(self):
        """get_health returns healthy=False for OPEN state model."""
        bridge = _make_bridge_with_cooldown({
            "deepseek-v4-flash": {
                "state": "OPEN",
                "state_value": 1,
                "failure_count": 5,
                "last_failure_time": 1000.0,
                "last_success_time": 900.0,
                "cooldown_until": 9999.0,
            },
        })
        sel = ModelSelector(bridge=bridge, config={})
        health = sel.get_health("deepseek-v4-flash")

        assert health["healthy"] is False
        assert health["failure_count"] == 5
        assert health["state"] == "OPEN"

    def test_get_health_missing_model_returns_default(self):
        """get_health returns default healthy response for unknown model."""
        bridge = _make_bridge_with_cooldown({
            "deepseek-v4-flash": {
                "state": "CLOSED",
                "state_value": 0,
                "failure_count": 0,
            },
        })
        sel = ModelSelector(bridge=bridge, config={})
        health = sel.get_health("unknown-model")

        assert health == {"healthy": True, "failure_count": 0, "state": "CLOSED"}

    def test_get_health_no_cooldown_tracker_returns_default(self):
        """get_health returns default when no cooldown tracker exists."""
        bridge = MagicMock()
        bridge._cooldown_tracker = None
        sel = ModelSelector(bridge=bridge, config={})
        health = sel.get_health("any-model")

        assert health == {"healthy": True, "failure_count": 0, "state": "CLOSED"}

    def test_get_health_cooldown_exception_returns_default(self):
        """get_health returns default when get_all_status raises exception."""
        bridge = MagicMock()
        cooldown = MagicMock()
        cooldown.get_all_status.side_effect = Exception("test error")
        bridge._cooldown_tracker = cooldown
        sel = ModelSelector(bridge=bridge, config={})
        health = sel.get_health("any-model")

        assert health == {"healthy": True, "failure_count": 0, "state": "CLOSED"}

    def test_get_health_cooldown_returns_empty_dict_returns_default(self):
        """get_health returns default when cooldown returns empty dict."""
        bridge = _make_bridge_with_cooldown({})
        sel = ModelSelector(bridge=bridge, config={})
        health = sel.get_health("deepseek-v4-flash")

        assert health == {"healthy": True, "failure_count": 0, "state": "CLOSED"}

    def test_get_health_cooldown_returns_none_returns_default(self):
        """get_health returns default when cooldown returns None."""
        bridge = MagicMock()
        cooldown = MagicMock()
        cooldown.get_all_status.return_value = None
        bridge._cooldown_tracker = cooldown
        sel = ModelSelector(bridge=bridge, config={})
        health = sel.get_health("any-model")

        assert health == {"healthy": True, "failure_count": 0, "state": "CLOSED"}

    def test_get_health_missing_state_field_defaults_to_closed(self):
        """If state field is missing in cooldown entry, defaults to CLOSED."""
        bridge = _make_bridge_with_cooldown({
            "deepseek-v4-flash": {
                "state_value": 0,
                "failure_count": 0,
            },
        })
        sel = ModelSelector(bridge=bridge, config={})
        health = sel.get_health("deepseek-v4-flash")

        assert health["state"] == "CLOSED"
        assert health["healthy"] is True

    def test_get_health_failure_count_zero_is_healthy(self):
        """A model with state CLOSED and zero failures is healthy."""
        bridge = _make_bridge_with_cooldown({
            "deepseek-v4-flash": {
                "state": "CLOSED",
                "state_value": 0,
                "failure_count": 0,
            },
        })
        sel = ModelSelector(bridge=bridge, config={})
        health = sel.get_health("deepseek-v4-flash")

        assert health["healthy"] is True
        assert health["failure_count"] == 0

    def test_get_health_multiple_failures_still_healthy_if_closed(self):
        """Model can have failures but still be healthy if state is CLOSED."""
        bridge = _make_bridge_with_cooldown({
            "deepseek-v4-flash": {
                "state": "CLOSED",
                "state_value": 0,
                "failure_count": 3,
            },
        })
        sel = ModelSelector(bridge=bridge, config={})
        health = sel.get_health("deepseek-v4-flash")

        assert health["healthy"] is True
        assert health["failure_count"] == 3
        assert health["state"] == "CLOSED"
