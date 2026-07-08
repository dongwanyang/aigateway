"""Shared pre-routing layer (总 1).

See ``docs/superpowers/specs/2026-07-07-runtime-structure-design.md``.
Everything under this package runs *before* dispatch splits the request
into an understanding or generation pipeline: PII, cache lookup/backfill,
media preprocessing.

Subpackages (``pii``, ``cache``, ``media``) are exposed lazily via
``__getattr__`` so that importing ``aigateway_core.prefix`` does not
eagerly pull in heavy deps (lz4, cachetools) or trigger circular imports
through the pipeline. Access ``aigateway_core.prefix.cache`` etc. and the
subpackage is imported on first access.
"""
from __future__ import annotations

import importlib as _importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from aigateway_core.prefix import cache, media, pii  # noqa: F401

_LAZY_SUBMODULES = {"pii", "cache", "media"}


def __getattr__(name: str):
    if name in _LAZY_SUBMODULES:
        mod = _importlib.import_module(f"{__name__}.{name}")
        globals()[name] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["pii", "cache", "media"]
