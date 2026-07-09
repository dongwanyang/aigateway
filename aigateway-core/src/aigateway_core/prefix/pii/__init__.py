"""PII detection / sanitize / reject — part of the shared prefix layer (总 1).

Authoritative implementation:
- ``aigateway_core.prefix.pii.detector`` — PIIDetector + pattern lists
- ``aigateway_core.prefix.plugins.classic_plugins`` — pipeline plugins
"""
from __future__ import annotations

from aigateway_core.prefix.pii.detector import PIIDetector  # noqa: F401

__all__ = ["PIIDetector"]
