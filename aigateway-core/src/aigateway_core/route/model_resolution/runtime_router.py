"""Runtime model selection after policy constraints have been applied."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .policy_engine import RoutingConstraints


@dataclass(frozen=True)
class RuntimeRouteDecision:
    model: str
    fallback_models: tuple[str, ...]
    reason: str
    excluded_unhealthy: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "selected_model": self.model,
            "fallback_models": list(self.fallback_models),
            "router_reason": self.reason,
            "excluded_unhealthy": list(self.excluded_unhealthy),
        }


class RuntimeModelRouter:
    """Rank policy-eligible models using live health, quality and cost."""

    def route(
        self,
        constraints: RoutingConstraints,
        health: Mapping[str, Mapping[str, Any]],
        pricing: Mapping[str, Mapping[str, float]],
        capability_scores: Mapping[str, int],
        latency_ms: Mapping[str, float],
    ) -> RuntimeRouteDecision:
        eligible = list(constraints.eligible_models)
        if not eligible:
            raise ValueError("runtime router received no eligible models")

        unhealthy = [
            model
            for model in eligible
            if str((health.get(model) or {}).get("state", "CLOSED")).upper() == "OPEN"
        ]
        healthy = [model for model in eligible if model not in unhealthy]
        active = healthy or eligible
        preference_rank = {
            model: index for index, model in enumerate(constraints.preferred_models)
        }

        def rank(model: str) -> tuple[Any, ...]:
            status = health.get(model) or {}
            failures = int(status.get("failure_count", 0) or 0)
            price = pricing.get(model)
            total_price = (
                float("inf")
                if not price
                else float(price.get("prompt", 0) or 0)
                + float(price.get("completion", 0) or 0)
            )
            capability = int(capability_scores.get(model, 50))
            complexity_gap = max(0, constraints.task_profile.complexity - capability)
            latency = float(latency_ms.get(model, float("inf")))
            return (
                complexity_gap,
                failures,
                preference_rank.get(model, len(preference_rank) + 1),
                latency,
                total_price,
                -capability,
                eligible.index(model),
            )

        ordered = sorted(active, key=rank)
        selected = ordered[0]
        fallbacks = tuple(ordered[1:])
        reason = "all_models_unhealthy" if not healthy else "runtime_ranked"
        return RuntimeRouteDecision(
            selected,
            fallbacks,
            reason,
            tuple(unhealthy),
        )
