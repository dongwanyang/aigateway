"""Cost estimation for request metrics and quota accounting.

Model prices are read from ``config.yaml`` under
``providers.*.model_grouper[].pricing``. This module deliberately contains no
built-in provider price table, preventing stale duplicated prices.
"""
from __future__ import annotations

import logging

from aigateway_core.shared.runtime_values import configured_model_pricing

logger = logging.getLogger(__name__)


def _estimate_cost(model: str, total_tokens: int) -> float:
    """Estimate cost using the model's configured prompt-token price.

    This function retains the previous total-token estimate semantics because it
    receives no prompt/completion split. Missing pricing returns ``0.0`` rather
    than inventing a fallback rate; the model must be priced in ``config.yaml``
    for quota and reporting estimates to include cost.
    """
    pricing = configured_model_pricing(model)
    if pricing is None:
        logger.warning("model pricing is not configured: %s", model)
        return 0.0
    return round(max(0, int(total_tokens)) * pricing["prompt"], 6)
