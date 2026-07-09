"""Verify aigateway_core.dispatch re-exports PipelineContext."""
import importlib


def test_dispatch_context_reexports():
    """dispatch.context is the canonical home for PipelineContext."""
    dispatch_ctx = importlib.import_module("aigateway_core.dispatch.context")
    # Verify it exports the expected public names
    assert hasattr(dispatch_ctx, "PipelineContext")
    assert hasattr(dispatch_ctx, "NS_RAG_RETRIEVER")


def test_dispatch_top_level_pipeline_context():
    from aigateway_core import dispatch
    from aigateway_core.dispatch.context import PipelineContext

    assert dispatch.PipelineContext is PipelineContext
