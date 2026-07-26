"""Verify aigateway_core.shared exports are accessible from new paths."""
import importlib


# After the skeleton refactor, shared modules live exclusively under
# aigateway_core.shared.*.  The old top-level shims (aigateway_core.config,
# aigateway_core.tracing, etc.) were removed — this test verifies the
# canonical paths are importable and export the expected symbols.

SHARED_MODULES = [
    "aigateway_core.shared.config",
    "aigateway_core.shared.tracing",
    "aigateway_core.shared.trace_event",
    "aigateway_core.shared.exceptions",
    "aigateway_core.shared.plugin_registry",
    "aigateway_core.shared.logger",
    "aigateway_core.shared.metrics",
    "aigateway_core.shared.debug_config",
    "aigateway_core.shared.redis_client",
    "aigateway_core.shared.qdrant_client",
    "aigateway_core.shared.integration_configs",
]


def test_shared_modules_are_importable():
    """All shared modules should be importable from their canonical paths."""
    for mod_path in SHARED_MODULES:
        mod = importlib.import_module(mod_path)
        exported = [name for name in dir(mod) if not name.startswith("_")]
        assert exported, f"{mod_path} has no public names to re-export"
