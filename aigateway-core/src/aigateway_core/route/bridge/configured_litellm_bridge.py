"""Config-aware LiteLLM bridge.

The large compatibility implementation lives in ``_litellm_bridge_impl.py``.
This subclass centralizes cost semantics without mutating class methods during
package import.
"""
from __future__ import annotations

import logging
from typing import Any

from aigateway_core.route.metrics.costing import estimate_model_cost

from ._litellm_bridge_impl import LiteLLMBridge as _BaseLiteLLMBridge

logger = logging.getLogger(__name__)


class ConfiguredLiteLLMBridge(_BaseLiteLLMBridge):
    """LiteLLM bridge using config-backed, split-token cost accounting."""

    def _track_usage(self, model: str, response: dict[str, Any]) -> float | None:
        usage = response.get("usage", {}) if isinstance(response, dict) else {}
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        total_tokens = int(
            usage.get("total_tokens", prompt_tokens + completion_tokens) or 0
        )

        if self.cost_tracker is not None:
            self.cost_tracker.total_input_tokens += prompt_tokens
            self.cost_tracker.total_output_tokens += completion_tokens
            self.cost_tracker.total_tokens += total_tokens

        estimate = estimate_model_cost(model, prompt_tokens, completion_tokens)
        if self.cost_tracker is not None and estimate.amount_usd is not None:
            self.cost_tracker.total_cost += estimate.amount_usd

        if isinstance(response, dict):
            response.setdefault("_meta", {})["pricing_status"] = estimate.status

        if estimate.amount_usd is None:
            logger.warning(
                "usage tracked with unknown pricing: model=%s, tokens_in=%d, "
                "tokens_out=%d",
                model,
                prompt_tokens,
                completion_tokens,
            )
        else:
            logger.debug(
                "usage tracked: model=%s, tokens_in=%d, tokens_out=%d, "
                "cost=$%.6f, pricing_status=%s",
                model,
                prompt_tokens,
                completion_tokens,
                estimate.amount_usd,
                estimate.status,
            )
        return estimate.amount_usd

    def _estimate_cost(self, model: str, total_tokens: int) -> float | None:
        """Compatibility API for callers that only know total token count."""
        return estimate_model_cost(model, total_tokens, 0).amount_usd


__all__ = ["ConfiguredLiteLLMBridge"]
