"""Generation pipeline — optimizes generation requests for cost/success/fit.

Six functional groups matching the six existing generation plugins plus the
strategies each depends on. See
``docs/superpowers/specs/2026-07-07-runtime-structure-design.md``.
"""
from aigateway_core.pipelines.generation import (  # noqa: F401
    director,
    intent,
    token,
    draft,
    cost,
    routing_signals,
)

__all__ = ["director", "intent", "token", "draft", "cost", "routing_signals"]
