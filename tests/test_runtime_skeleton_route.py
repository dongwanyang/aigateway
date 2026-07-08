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
        assert getattr(new_mod, name) is getattr(src_mod, name)


def test_route_model_resolution_reexports():
    from aigateway_core.route import model_resolution
    _assert_identical(
        model_resolution,
        "aigateway_core.generation_optimization.strategies.model_router",
    )


def test_route_bridge_reexports():
    from aigateway_core.route import bridge
    _assert_identical(bridge, "aigateway_core.litellm_bridge")


def test_route_all_lists_expected_subpackages():
    from aigateway_core import route

    assert sorted(route.__all__) == ["bridge", "model_resolution"]
