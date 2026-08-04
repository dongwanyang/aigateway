"""Provider-model reference cleanup shared by secure config write routes."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .config_security import ConfigValidationError


def configured_model_names(config: Mapping[str, Any]) -> set[str]:
    """Return model names explicitly declared by provider model groups."""
    providers = config.get("providers")
    if not isinstance(providers, Mapping):
        return set()
    names: set[str] = set()
    for provider in providers.values():
        if not isinstance(provider, Mapping):
            continue
        groups = provider.get("model_grouper")
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, Mapping):
                continue
            models = group.get("models")
            if not isinstance(models, list):
                continue
            for model in models:
                if isinstance(model, str):
                    name = model.strip()
                elif isinstance(model, Mapping):
                    name = str(model.get("name") or "").strip()
                else:
                    name = ""
                if name:
                    names.add(name)
    return names


def _mapping_path(config: Mapping[str, Any], *parts: str) -> Any:
    current: Any = config
    for part in parts:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _scalar_references(
    config: Mapping[str, Any],
    removed_models: set[str],
) -> dict[str, list[str]]:
    """Return mandatory scalar references that cannot be pruned safely."""
    references: dict[str, list[str]] = {}

    def add(path: str, value: Any) -> None:
        if not isinstance(value, str):
            return
        model = value.strip()
        if model in removed_models:
            references.setdefault(model, []).append(path)

    add(
        "intent_classifier.model",
        _mapping_path(config, "intent_classifier", "model"),
    )
    add(
        "generation_optimization.ai_director.rewrite_model",
        _mapping_path(
            config,
            "generation_optimization",
            "ai_director",
            "rewrite_model",
        ),
    )
    add(
        "generation_optimization.model_router.default_model",
        _mapping_path(
            config,
            "generation_optimization",
            "model_router",
            "default_model",
        ),
    )
    add(
        "generation_optimization.draft_workflow.draft_model",
        _mapping_path(
            config,
            "generation_optimization",
            "draft_workflow",
            "draft_model",
        ),
    )
    add(
        "media_optimization.image.caption_model",
        _mapping_path(
            config,
            "media_optimization",
            "image",
            "caption_model",
        ),
    )

    plugins = config.get("plugins")
    if isinstance(plugins, list):
        for index, plugin in enumerate(plugins):
            if not isinstance(plugin, Mapping):
                continue
            if plugin.get("name") != "conv_compressor":
                continue
            plugin_config = plugin.get("config")
            if isinstance(plugin_config, Mapping):
                add(
                    f"plugins.{index}.config.summary_model",
                    plugin_config.get("summary_model"),
                )
    return references


def prune_removed_model_references(
    config: dict[str, Any],
    removed_models: set[str],
) -> None:
    """Prune safe references and reject dangling mandatory scalar references.

    Lists and maps can remove a model without inventing a replacement. Scalar
    runtime dependencies require an explicit operator choice, so deletion is
    rejected with exact paths until those fields are changed to another model.
    """
    if not removed_models:
        return

    blocked = _scalar_references(config, removed_models)
    if blocked:
        issues = []
        for model in sorted(blocked):
            paths = ", ".join(sorted(blocked[model]))
            issues.append(
                {
                    "level": "ERROR",
                    "message": (
                        f"model {model!r} cannot be removed while referenced by "
                        f"{paths}; select a replacement model first"
                    ),
                }
            )
        raise ConfigValidationError(issues)

    providers = config.get("providers")
    if isinstance(providers, dict):
        for provider in providers.values():
            if not isinstance(provider, dict):
                continue
            groups = provider.get("model_grouper")
            if not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, dict):
                    continue
                fallbacks = group.get("fallback_models")
                if isinstance(fallbacks, list):
                    group["fallback_models"] = [
                        item
                        for item in fallbacks
                        if not (
                            isinstance(item, str)
                            and item.strip() in removed_models
                        )
                    ]
                pricing = group.get("pricing")
                if isinstance(pricing, dict):
                    for model_name in removed_models:
                        pricing.pop(model_name, None)

    task_routing = config.get("task_routing")
    if isinstance(task_routing, dict):
        preferences = task_routing.get("model_preferences")
        if isinstance(preferences, dict):
            for task, models in list(preferences.items()):
                if isinstance(models, list):
                    preferences[task] = [
                        item
                        for item in models
                        if not (
                            isinstance(item, str)
                            and item.strip() in removed_models
                        )
                    ]

    generation = config.get("generation_optimization")
    if isinstance(generation, dict):
        router_config = generation.get("model_router")
        if isinstance(router_config, dict):
            for field in ("model_capabilities", "model_modalities"):
                keyed = router_config.get(field)
                if isinstance(keyed, dict):
                    for model_name in removed_models:
                        keyed.pop(model_name, None)


__all__ = [
    "configured_model_names",
    "prune_removed_model_references",
]
