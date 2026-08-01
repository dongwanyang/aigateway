"""Cost estimation for request metrics and quota accounting.

Model prices are read from ``config.yaml`` under
``providers.*.model_grouper[].pricing``. This module deliberately contains no
built-in provider price table, preventing stale duplicated prices.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from aigateway_core.shared.runtime_values import configured_model_pricing

logger = logging.getLogger(__name__)

PricingStatus = Literal["priced", "free", "unpriced"]


@dataclass(frozen=True)
class CostEstimate:
    """Result of a model cost lookup.

    ``amount_usd`` is ``None`` when no pricing entry exists. A configured model
    whose prompt and completion prices are both zero is represented as
    ``status='free'`` with ``amount_usd=0.0``.
    """

    amount_usd: float | None
    status: PricingStatus
    prompt_tokens: int
    completion_tokens: int


class PricingCost(float):
    """Numeric cost carrying whether the model was priced, free or unpriced.

    Internal dispatcher, quota and SQLite arithmetic expects a numeric value.
    An unpriced model therefore uses numeric value ``0.0`` only on this internal
    adapter while retaining ``pricing_status='unpriced'``. Strict lookup callers
    must use :func:`estimate_model_cost` or :func:`_estimate_cost`, both of which
    preserve ``None`` for an unknown price.
    """

    pricing_status: PricingStatus
    pricing_known: bool

    def __new__(cls, estimate: CostEstimate):
        value = estimate.amount_usd if estimate.amount_usd is not None else 0.0
        instance = super().__new__(cls, value)
        instance.pricing_status = estimate.status
        instance.pricing_known = estimate.amount_usd is not None
        return instance


def estimate_model_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> CostEstimate:
    """Calculate cost from configured prompt/completion token prices."""
    prompt_count = max(0, int(prompt_tokens))
    completion_count = max(0, int(completion_tokens))
    pricing = configured_model_pricing(model)
    if pricing is None:
        logger.warning("model pricing is not configured: %s", model)
        return CostEstimate(
            amount_usd=None,
            status="unpriced",
            prompt_tokens=prompt_count,
            completion_tokens=completion_count,
        )

    prompt_price = float(pricing["prompt"])
    completion_price = float(pricing["completion"])
    amount = round(
        prompt_count * prompt_price + completion_count * completion_price,
        6,
    )
    status: PricingStatus = (
        "free" if prompt_price == 0.0 and completion_price == 0.0 else "priced"
    )
    return CostEstimate(
        amount_usd=amount,
        status=status,
        prompt_tokens=prompt_count,
        completion_tokens=completion_count,
    )


def numeric_cost(estimate: CostEstimate) -> PricingCost:
    """Adapt a structured estimate to the internal numeric accounting interface."""
    return PricingCost(estimate)


def _estimate_cost(model: str, total_tokens: int) -> float | None:
    """Return a strict configured cost for callers without a token split.

    All tokens are treated as prompt tokens. Missing pricing remains ``None``;
    explicitly configured free models return ``0.0``.
    """
    return estimate_model_cost(model, total_tokens, 0).amount_usd


__all__ = [
    "CostEstimate",
    "PricingCost",
    "PricingStatus",
    "_estimate_cost",
    "estimate_model_cost",
    "numeric_cost",
]
