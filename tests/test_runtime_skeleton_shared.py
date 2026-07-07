"""Verify aigateway_core.shared re-exports the same objects as the legacy paths."""
import importlib


PAIRS = [
    ("aigateway_core.config", "aigateway_core.shared.config"),
    ("aigateway_core.tracing", "aigateway_core.shared.tracing"),
    ("aigateway_core.trace_event", "aigateway_core.shared.trace_event"),
    ("aigateway_core.exceptions", "aigateway_core.shared.exceptions"),
    ("aigateway_core.plugin_registry", "aigateway_core.shared.plugin_registry"),
    ("aigateway_core.logger", "aigateway_core.shared.logger"),
    ("aigateway_core.metrics", "aigateway_core.shared.metrics"),
    ("aigateway_core.debug_config", "aigateway_core.shared.debug_config"),
    ("aigateway_core.redis_client", "aigateway_core.shared.redis_client"),
    ("aigateway_core.qdrant_client", "aigateway_core.shared.qdrant_client"),
    ("aigateway_core.integration_configs", "aigateway_core.shared.integration_configs"),
]


def test_shared_reexports_are_identical():
    for old, new in PAIRS:
        old_mod = importlib.import_module(old)
        new_mod = importlib.import_module(new)
        exported = [name for name in dir(old_mod) if not name.startswith("_")]
        assert exported, f"{old} has no public names to re-export"
        for name in exported:
            assert hasattr(new_mod, name), f"{new} missing {name!r} re-exported from {old}"
            assert getattr(new_mod, name) is getattr(old_mod, name), (
                f"{new}.{name} is not the same object as {old}.{name}"
            )
