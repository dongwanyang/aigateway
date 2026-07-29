"""Policy constraints between semantic classification and runtime routing."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .task_classifier import REQUIREMENTS, TASKS, TaskProfile


class RoutingPolicyConfigError(ValueError):
    """Raised when routing policy configuration is unsafe or ambiguous."""


class NoModelSatisfiesPolicy(ValueError):
    """Raised when a hard runtime requirement has no capable model."""


@dataclass(frozen=True)
class RoutingConstraints:
    """Policy output.  It intentionally does not contain a selected model."""

    task_profile: TaskProfile
    eligible_models: tuple[str, ...]
    preferred_models: tuple[str, ...]
    reason: str
    unmet_requirements: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible_models": list(self.eligible_models),
            "preferred_models": list(self.preferred_models),
            "policy_reason": self.reason,
            "unmet_requirements": list(self.unmet_requirements),
        }


class RoutingPolicyEngine:
    """Apply tenant/product policy and return constraints for the Router."""

    _SELECTION_MODES = {"strict", "policy", "auto"}

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self.enabled = self._boolean(config.get("enabled", False), "enabled")
        self.version = str(config.get("version", "1")).strip()
        if not self.version:
            raise RoutingPolicyConfigError("task_routing.version must not be empty")

        self.min_confidence = self._number(
            config.get("min_confidence", 0.6), "min_confidence"
        )
        if not 0.0 <= self.min_confidence <= 1.0:
            raise RoutingPolicyConfigError(
                "task_routing.min_confidence must be in [0, 1]"
            )

        self.model_selection_mode = str(
            config.get("model_selection_mode", "policy")
        ).lower()
        if self.model_selection_mode not in self._SELECTION_MODES:
            raise RoutingPolicyConfigError(
                "task_routing.model_selection_mode must be strict, policy, or auto"
            )
        self.expose_debug_metadata = self._boolean(
            config.get("expose_debug_metadata", False),
            "expose_debug_metadata",
        )

        raw_preferences = config.get("model_preferences", {})
        if raw_preferences is None:
            raw_preferences = {}
        if not isinstance(raw_preferences, dict):
            raise RoutingPolicyConfigError(
                "task_routing.model_preferences must be an object"
            )
        preferences: dict[str, tuple[str, ...]] = {}
        for task, models in raw_preferences.items():
            task_name = str(task).lower()
            if task_name not in TASKS:
                raise RoutingPolicyConfigError(
                    f"task_routing.model_preferences has unknown task '{task}'"
                )
            if not isinstance(models, list) or not all(
                isinstance(model, str) and model.strip() for model in models
            ):
                raise RoutingPolicyConfigError(
                    f"task_routing.model_preferences.{task} must be a list of model names"
                )
            preferences[task_name] = tuple(dict.fromkeys(models))
        self.preferences = preferences

    def constrain(
        self,
        profile: TaskProfile,
        candidates: Sequence[str],
        model_hint: str | None,
        model_tasks: Mapping[str, Sequence[str]],
        model_features: Mapping[str, Sequence[str]] | None = None,
    ) -> RoutingConstraints:
        pool = tuple(dict.fromkeys(candidates))
        if not pool:
            raise ValueError("policy candidates must not be empty")
        features = model_features or {}

        if self.model_selection_mode == "strict" and model_hint in pool:
            return RoutingConstraints(
                profile,
                (str(model_hint),),
                (str(model_hint),),
                "strict_model_contract",
            )

        if not self.enabled or profile.confidence < self.min_confidence:
            preferred = (model_hint,) if model_hint in pool else ()
            return RoutingConstraints(
                profile,
                pool,
                preferred,
                "low_confidence_fallback" if self.enabled else "policy_disabled",
            )

        feature_eligible = pool
        unmet: list[str] = []
        for requirement in profile.requirements:
            if requirement not in REQUIREMENTS:
                continue
            supporting = tuple(
                model
                for model in feature_eligible
                if requirement in features.get(model, ())
            )
            if supporting:
                feature_eligible = supporting
            else:
                unmet.append(requirement)
        if unmet:
            raise NoModelSatisfiesPolicy(
                "no eligible model declares required features: "
                + ", ".join(unmet)
            )

        task_eligible = tuple(
            model
            for model in feature_eligible
            if profile.operation in model_tasks.get(model, ())
            or "*" in model_tasks.get(model, ())
        )
        eligible = task_eligible or feature_eligible
        reason = "task_constrained" if task_eligible else "task_metadata_unconfigured"

        configured = tuple(
            model for model in self.preferences.get(profile.operation, ()) if model in eligible
        )
        if self.model_selection_mode == "auto":
            # Auto mode ignores explicit model hints; only use configured preferences.
            preferred = configured
        else:
            preferred = configured
            if model_hint in eligible and model_hint not in preferred:
                preferred = (*preferred, str(model_hint))

        return RoutingConstraints(
            profile,
            tuple(eligible),
            tuple(preferred),
            reason,
            tuple(unmet),
        )

    def validate_models(
        self,
        registered_models: Sequence[str],
        model_tasks: Mapping[str, Sequence[str]],
        model_features: Mapping[str, Sequence[str]],
    ) -> None:
        registered = set(registered_models)
        unknown_preferences = sorted(
            model
            for models in self.preferences.values()
            for model in models
            if model not in registered
        )
        if unknown_preferences:
            raise RoutingPolicyConfigError(
                "task_routing references unregistered models: "
                + ", ".join(unknown_preferences)
            )
        for model, tasks in model_tasks.items():
            unknown = sorted(set(tasks) - set(TASKS) - {"*"})
            if unknown:
                raise RoutingPolicyConfigError(
                    f"model '{model}' has unknown tasks: {', '.join(unknown)}"
                )
        for model, features in model_features.items():
            unknown = sorted(set(features) - set(REQUIREMENTS))
            if unknown:
                raise RoutingPolicyConfigError(
                    f"model '{model}' has unknown features: {', '.join(unknown)}"
                )

    @staticmethod
    def _boolean(value: Any, field: str) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        raise RoutingPolicyConfigError(f"task_routing.{field} must be a boolean")

    @staticmethod
    def _number(value: Any, field: str) -> float:
        if isinstance(value, bool):
            raise RoutingPolicyConfigError(f"task_routing.{field} must be numeric")
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise RoutingPolicyConfigError(
                f"task_routing.{field} must be numeric"
            ) from exc
