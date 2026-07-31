from __future__ import annotations

from aigateway_api.config_management_routes import _merge_debug_section


def test_partial_debug_update_preserves_legacy_flat_master_flag() -> None:
    merged = _merge_debug_section(
        {
            "entry": False,
            "plugins_enabled": True,
            "plugins": {"per_plugin": {"pii_detector": True}},
        },
        {"entry": True},
    )

    assert merged["entry"] is True
    assert merged["plugins_enabled"] is True
    assert merged["plugins"]["enabled"] is True
    assert merged["plugins"]["per_plugin"]["pii_detector"] is True
