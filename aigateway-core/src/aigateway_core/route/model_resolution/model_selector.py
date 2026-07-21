"""ModelSelector — picks a cheap text-capable model for internal calls.

Internal pre-routing calls (intent pre-judge, ``ai_director``) need a concrete
model but must not enter the routing loop (auto-resolver would re-dispatch them
back through the same intent classifier → infinite recursion). This selector
picks the cheapest *healthy* text model from the bridge's registered pool using
the REAL ``ProviderCooldownTracker.get_all_status()`` API, which returns a dict
keyed by model name with ``{state, state_value, failure_count, last_failure_time,
last_success_time, cooldown_until}``.

Scoring:
    health_score = 1.0 / (1.0 + failure_count)
    cost         = pricing[m].prompt + pricing[m].completion
    score        = success_weight * health_score + cost_weight * (1.0 / (1.0 + cost))

``latency_weight`` in config is accepted but ignored — there is no real latency
data source today; keeping it in the config schema avoids surprising downstream
callers that set it.

Guarantees:
    * ``select_text_model`` NEVER raises — on timeout or any exception it logs a
      warning and returns ``default_model``.
    * Returns a bare model name string.
    * If every text model is OPEN/unhealthy, returns ``text_pool[0]`` so internal
      calls still proceed (the bridge's own fallback chain handles the failure).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ModelSelector:
    """Health/cost-weighted selector for internal text-model calls."""

    def __init__(
        self,
        bridge: Any,
        config: Optional[Dict[str, Any]] = None,
        default_model: str = "agnes-2.0-flash",
        timeout_seconds: float = 0.5,
    ) -> None:
        self._bridge = bridge
        cfg = config or {}
        self._success_rate_weight = float(cfg.get("success_rate_weight", 0.4))
        self._cost_weight = float(cfg.get("cost_weight", 0.2))
        # Accepted for API compatibility; no real latency data source exists.
        # Deliberately NOT used in scoring.
        self._latency_weight = float(cfg.get("latency_weight", 0.0))
        self._default_model = default_model
        self._timeout = timeout_seconds

    async def select_text_model(self) -> str:
        """Return a bare model name. Never raises."""
        try:
            return await asyncio.wait_for(self._select(), timeout=self._timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "model_selector: timed out after %.2fs, returning default %s",
                self._timeout,
                self._default_model,
            )
            return self._default_model
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "model_selector: error during selection (%s), returning default %s",
                exc,
                self._default_model,
            )
            return self._default_model

    async def _select(self) -> str:
        bridge = self._bridge
        caps: Dict[str, List[str]] = getattr(bridge, "_model_capabilities", {}) or {}
        registered: List[str] = bridge.get_registered_models() or []

        text_pool = [m for m in registered if "text" in caps.get(m, [])]
        if not text_pool:
            logger.warning(
                "model_selector: no text-capable models in pool, returning default %s",
                self._default_model,
            )
            return self._default_model

        cooldown = getattr(bridge, "_cooldown_tracker", None)
        status: Dict[str, Dict[str, Any]] = {}
        if cooldown is not None:
            try:
                status = cooldown.get_all_status() or {}
            except Exception as exc:
                logger.warning("model_selector: get_all_status failed (%s)", exc)
                status = {}

        pricing: Dict[str, Dict[str, float]] = getattr(bridge, "_model_pricing", {}) or {}

        best: Optional[str] = None
        best_score = -1.0
        for m in text_pool:
            entry = status.get(m)
            # Missing entry OR state OPEN → unhealthy, skip.
            if entry is None or entry.get("state") == "OPEN":
                continue
            failure_count = int(entry.get("failure_count", 0) or 0)
            health_score = 1.0 / (1.0 + failure_count)

            price = pricing.get(m, {}) or {}
            cost = float(price.get("prompt", 0) or 0) + float(price.get("completion", 0) or 0)
            cost_score = 1.0 / (1.0 + cost)

            score = self._success_rate_weight * health_score + self._cost_weight * cost_score
            if score > best_score:
                best_score = score
                best = m

        if best is None:
            logger.warning(
                "model_selector: all text models unhealthy, using pool first %s",
                text_pool[0],
            )
            return text_pool[0]

        return best

    def get_health(self, model: str) -> Dict[str, Any]:
        """Return ``{healthy, failure_count, state}`` for ``model``.

        Falls back to a fully-healthy sentinel when no cooldown tracker exists,
        the model is missing, or any error occurs.
        """
        healthy_default = {"healthy": True, "failure_count": 0, "state": "CLOSED"}
        cooldown = getattr(self._bridge, "_cooldown_tracker", None)
        if cooldown is None:
            return healthy_default
        try:
            status = cooldown.get_all_status() or {}
        except Exception as exc:
            logger.warning("model_selector.get_health: get_all_status failed (%s)", exc)
            return healthy_default
        entry = status.get(model)
        if not entry:
            return healthy_default
        state = entry.get("state", "CLOSED")
        return {
            "healthy": state != "OPEN",
            "failure_count": int(entry.get("failure_count", 0) or 0),
            "state": state,
        }
