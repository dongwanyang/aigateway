"""Dispatch layer (总 1 后半段).

Exposes the shared ``PipelineContext``, ``classify_request``, and the
``RequestDispatcher`` entry orchestrator under their runtime-layer home.
``aigateway_api.dispatcher`` is now a thin adapter that re-exports from here.
"""
from aigateway_core.dispatch.classifier import classify_request
from aigateway_core.dispatch.context import (
    PipelineContext,
    PluginContext,
    RequestContext,
)
from aigateway_core.dispatch.pipeline_engine import PipelineEngine

__all__ = [
    "PipelineContext",
    "PipelineEngine",
    "PluginContext",
    "RequestContext",
    "classify_request",
]
