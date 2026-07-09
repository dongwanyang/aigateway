"""Verify aigateway_core.dispatch re-exports PipelineContext."""
import importlib


def test_dispatch_context_reexports():
    dispatch_ctx = importlib.import_module("aigateway_core.dispatch.context")
    legacy_ctx = importlib.import_module("aigateway_core.context")
    for name in dir(legacy_ctx):
        if name.startswith("_"):
            continue
        assert hasattr(dispatch_ctx, name)
        assert getattr(dispatch_ctx, name) is getattr(legacy_ctx, name)


def test_dispatch_top_level_pipeline_context():
    from aigateway_core import dispatch
    from aigateway_core.dispatch.context import PipelineContext

    assert dispatch.PipelineContext is PipelineContext
