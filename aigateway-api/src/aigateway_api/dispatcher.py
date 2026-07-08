"""RequestDispatcher adapter (thin re-export).

The real implementation now lives in ``aigateway_core.dispatch.dispatcher``.
This module remains as a backward-compatible import surface for callers that
still import from ``aigateway_api.dispatcher`` (e.g. ``openai_compat.py``).
"""

from aigateway_core.dispatch.classifier import classify_request
from aigateway_core.dispatch.dispatcher import RequestDispatcher

__all__ = ["RequestDispatcher", "classify_request"]
