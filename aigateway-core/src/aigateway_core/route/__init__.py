"""Unified routing + response closure (总 2).

See ``docs/superpowers/specs/2026-07-07-runtime-structure-design.md``.
This layer covers model resolution, LiteLLM bridge, provider fallback,
streaming/response assembly, and quota/metrics closure. Model
resolution and bridge are already migrated; streaming and metrics
helpers moved here from the API surface in Task 5.

The subpackages ``model_resolution`` and ``bridge`` are imported lazily
via ``__getattr__`` so that importing a leaf module (e.g.
``aigateway_core.route.bridge.cooldown``) does not force eager loading
of sibling subpackages — which previously triggered a circular import
through ``model_resolution`` → ``generation_optimization.strategies``
→ ``context`` → ``dispatch`` → ``plugin_registry``.
``streaming`` and ``metrics`` are also lazy-loaded for consistency.
"""
import importlib

__all__ = ["model_resolution", "bridge", "streaming", "metrics"]


def __getattr__(name):
    if name in __all__:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(list(globals().keys()) + __all__)
