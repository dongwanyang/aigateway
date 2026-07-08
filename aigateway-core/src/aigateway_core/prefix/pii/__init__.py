"""PII detection / sanitize / reject — part of the shared prefix layer (总 1).

Authoritative implementation:
- ``aigateway_core.prefix.pii.detector`` — PIIDetector + pattern lists
- ``aigateway_core.pipeline.PIIDetectorPlugin`` — pipeline plugin (lazy)

``PIIDetector`` is imported eagerly (lightweight, no circular risk).
``PIIDetectorPlugin`` is exposed lazily via ``__getattr__`` because importing
``aigateway_core.pipeline`` during package init triggers a circular import
(pipeline → security shim → prefix.pii.detector → this package).
"""
from __future__ import annotations

import importlib as _importlib

from aigateway_core.prefix.pii.detector import PIIDetector  # noqa: F401

_LAZY = {"PIIDetectorPlugin": "aigateway_core.pipeline"}


def __getattr__(name: str):
    if name in _LAZY:
        mod = _importlib.import_module(_LAZY[name])
        value = getattr(mod, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["PIIDetector", "PIIDetectorPlugin"]
