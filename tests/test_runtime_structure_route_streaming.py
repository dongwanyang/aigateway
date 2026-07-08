"""Task 4: route bridge migration.

Verifies that LiteLLMBridge and ProviderCooldownTracker are *defined* in the
route/bridge submodules (not merely re-exported from the root file).
"""
from aigateway_core.route.bridge.cooldown import ProviderCooldownTracker
from aigateway_core.route.bridge.litellm_bridge import LiteLLMBridge


def test_route_bridge_objects_resolve_from_new_paths():
    assert ProviderCooldownTracker.__name__ == "ProviderCooldownTracker"
    assert LiteLLMBridge.__name__ == "LiteLLMBridge"


def test_root_shim_reexports_same_objects():
    """The root compatibility shim must hand back the same class objects."""
    from aigateway_core import litellm_bridge as root

    assert root.LiteLLMBridge is LiteLLMBridge
    assert root.ProviderCooldownTracker is ProviderCooldownTracker
