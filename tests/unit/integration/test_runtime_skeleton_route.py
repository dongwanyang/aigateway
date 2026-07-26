"""Verify aigateway_core.route re-exports."""
import importlib


def _assert_identical(new_mod, src_path):
    src_mod = importlib.import_module(src_path)
    for name in dir(src_mod):
        if name.startswith("_"):
            continue
        assert hasattr(new_mod, name), (
            f"{new_mod.__name__} missing {name!r} from {src_path}"
        )
        assert getattr(new_mod, name) is getattr(src_mod, name), (
            f"{new_mod.__name__}.{name} differs from {src_path}.{name}"
        )


def test_route_model_resolution_reexports():
    from aigateway_core.route import model_resolution
    _assert_identical(
        model_resolution,
        "aigateway_core.route.model_resolution.model_router",
    )


def test_route_bridge_reexports():
    """bridge subpackage re-exports LiteLLMBridge and ProviderCooldownTracker."""
    from aigateway_core.route import bridge
    assert bridge.LiteLLMBridge is not None
    assert bridge.ProviderCooldownTracker is not None


def test_route_all_lists_expected_subpackages():
    from aigateway_core import route

    assert sorted(route.__all__) == ["bridge", "metrics", "model_resolution", "streaming"]
