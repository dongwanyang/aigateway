"""Public model-router surface with unit-consistent cost estimates.

The routing algorithm remains in ``_model_router_impl``. Provider pricing is
stored as USD/token, while the legacy ``ModelConfig.price_per_request`` field is
used for cheapest-model comparisons and exposed as an estimated request cost.
This facade converts the token rates to one documented representative request so
all routing comparisons and ``estimated_cost`` values use USD/request.
"""
from __future__ import annotations

import logging
from typing import Any

from . import _model_router_impl as _impl

logger = logging.getLogger(__name__)

# Static routing cannot know final usage before selecting a model. Use one stable
# representative request rather than mixing USD/token with USD/request. Actual
# billing continues to use measured prompt/completion tokens after completion.
_ROUTING_ESTIMATED_PROMPT_TOKENS = 1_000
_ROUTING_ESTIMATED_COMPLETION_TOKENS = 500

ModelConfig = _impl.ModelConfig


def _non_negative_rate(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if result > 0 else 0.0


def estimate_routing_request_cost(pricing: Any) -> float:
    """Convert one model's USD/token pricing to a representative USD/request."""
    if not isinstance(pricing, dict):
        return 0.0
    prompt_rate = _non_negative_rate(pricing.get("prompt", 0.0))
    completion_rate = _non_negative_rate(pricing.get("completion", 0.0))
    return (
        prompt_rate * _ROUTING_ESTIMATED_PROMPT_TOKENS
        + completion_rate * _ROUTING_ESTIMATED_COMPLETION_TOKENS
    )


class ModelRouterStrategy(_impl.ModelRouterStrategy):
    """Model router whose cost heuristic has explicit USD/request units."""

    def _build_model_list(self) -> list[ModelConfig]:
        models = super()._build_model_list()
        by_name = {model.name: model for model in models}

        for provider_data in (self.providers_config or {}).values():
            if not isinstance(provider_data, dict):
                continue
            groups = provider_data.get("model_grouper", [])
            if not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, dict):
                    continue
                pricing = group.get("pricing", {})
                if not isinstance(pricing, dict):
                    continue
                for model_name, model_pricing in pricing.items():
                    model = by_name.get(str(model_name))
                    if model is not None:
                        model.price_per_request = estimate_routing_request_cost(
                            model_pricing
                        )
        return models


# Re-export helpers and exceptions historically reachable from this module.
for _name in dir(_impl):
    if _name.startswith("__") or _name in {"ModelConfig", "ModelRouterStrategy"}:
        continue
    globals()[_name] = getattr(_impl, _name)


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


__all__ = (
    "ModelConfig",
    "ModelRouterStrategy",
    "estimate_routing_request_cost",
)
