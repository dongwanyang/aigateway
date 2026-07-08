"""Dispatch layer (总 1 后半段).

Exposes the shared ``PipelineContext``, ``classify_request``, and the
``RequestDispatcher`` entry orchestrator under their runtime-layer home.
``aigateway_api.dispatcher`` is now a thin adapter that re-exports from here.
"""
from aigateway_core.dispatch.classifier import classify_request  # noqa: F401
from aigateway_core.dispatch.context import PipelineContext  # noqa: F401
from aigateway_core.dispatch.pipeline_engine import PipelineEngine  # noqa: F401

__all__ = ["PipelineContext", "PipelineEngine", "classify_request"]
