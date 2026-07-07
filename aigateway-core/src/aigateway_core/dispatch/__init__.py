"""Dispatch layer (总 1 后半段).

The full dispatcher/classifier still lives in ``aigateway_api.dispatcher``
today; moving it into core is Phase 3 of the migration strategy in
``docs/superpowers/specs/2026-07-07-runtime-structure-design.md``.
This package currently exposes the shared ``PipelineContext`` under its
runtime-layer home.
"""
from aigateway_core.dispatch.context import PipelineContext  # noqa: F401

__all__ = ["PipelineContext"]
