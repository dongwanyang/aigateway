"""Generation pipeline - optimizes generation requests for cost/success/fit.

Six functional groups (director/intent/token/draft/cost/routing_signals) plus
a shared ``_common`` (config/models/metrics/exceptions/api_key_groups). Plugin
registration lives in ``registration.py`` and is imported lazily by the prefix
registration helper so importing this package stays lightweight. See
``docs/superpowers/specs/2026-07-07-runtime-structure-design.md``.
"""
from aigateway_core.pipelines.generation._common import *  # noqa: F401,F403
from aigateway_core.pipelines.generation._common import __all__ as _common_all

__all__ = list(_common_all)
