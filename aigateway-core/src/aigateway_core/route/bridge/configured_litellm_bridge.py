"""Config-aware LiteLLM bridge.

The large compatibility implementation lives in ``_litellm_bridge_impl.py``.
This subclass centralizes cost semantics without mutating class methods during
package import.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from aigateway_core.route.metrics.costing import (
    PricingCost,
    estimate_model_cost,
    numeric_cost,
)

from ._litellm_bridge_impl import LiteLLMBridge as _BaseLiteLLMBridge

logger = logging.getLogger(__name__)


class ConfiguredLiteLLMBridge(_BaseLiteLLMBridge):
    """LiteLLM bridge using config-backed, split-token cost accounting."""

    def _register_model_pricing(
        self,
        group: dict[str, Any],
        litellm_model: str,
        base_url: str | None,
        provider_name: str,
    ) -> None:
        """Register LiteLLM placeholders without treating unknown prices as free.

        The compatibility implementation registers a zero-cost placeholder when
        pricing is absent so LiteLLM does not emit unknown-model warnings. That
        placeholder is valid only for LiteLLM's internal registry; the gateway's
        runtime router must keep the model absent from ``_model_pricing`` so an
        unknown price ranks as infinity rather than as a configured free model.
        """
        super()._register_model_pricing(
            group,
            litellm_model,
            base_url,
            provider_name,
        )

        pricing = group.get("pricing")
        if not isinstance(pricing, dict):
            pricing = {}
        bare_model = litellm_model.split("/")[-1]
        candidates = (
            pricing.get(litellm_model),
            pricing.get(bare_model),
            pricing.get(provider_name),
            pricing.get("$default"),
        )
        configured_entry = next(
            (
                entry
                for entry in candidates
                if isinstance(entry, dict)
                and ("prompt" in entry or "completion" in entry)
            ),
            None,
        )
        if configured_entry is None:
            self._model_pricing.pop(litellm_model, None)
            self._model_pricing.pop(bare_model, None)

    def _track_usage(self, model: str, response: dict[str, Any]) -> PricingCost:
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
        cost = numeric_cost(estimate)
        if self.cost_tracker is not None and estimate.amount_usd is not None:
            self.cost_tracker.total_cost += estimate.amount_usd

        if isinstance(response, dict):
            metadata = response.get("_meta")
            if not isinstance(metadata, dict):
                metadata = {}
                response["_meta"] = metadata
            metadata["pricing_status"] = estimate.status

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
        return cost

    def _estimate_cost(self, model: str, total_tokens: int) -> float | None:
        """Return strict configured cost for callers with only a total count."""
        return estimate_model_cost(model, total_tokens, 0).amount_usd

    async def completion(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Propagate the structured pricing state to the outer bridge metadata."""
        result = await super().completion(*args, **kwargs)
        if not isinstance(result, dict):
            return result
        data = result.get("data")
        data_metadata = data.get("_meta") if isinstance(data, dict) else None
        pricing_status = (
            data_metadata.get("pricing_status")
            if isinstance(data_metadata, dict)
            else None
        )
        if pricing_status:
            outer_metadata = result.get("_meta")
            if not isinstance(outer_metadata, dict):
                outer_metadata = {}
                result["_meta"] = outer_metadata
            outer_metadata["pricing_status"] = pricing_status
        return result

    async def completion_stream(
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        """Expose pricing state on generated-media stream chunks."""
        async for chunk in super().completion_stream(*args, **kwargs):
            if isinstance(chunk, dict):
                metadata = chunk.get("_meta")
                if isinstance(metadata, dict):
                    status = getattr(metadata.get("cost"), "pricing_status", None)
                    if status:
                        metadata["pricing_status"] = status
            yield chunk


__all__ = ["ConfiguredLiteLLMBridge"]
