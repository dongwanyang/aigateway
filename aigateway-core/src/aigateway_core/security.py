"""Compatibility shim — real implementations moved to ``prefix`` and ``shared``.

This module re-exports the public names previously defined here so that
existing callers (``from aigateway_core.security import KeyStore`` etc.)
keep working. New code should import from the owning modules:

- ``aigateway_core.prefix.pii.detector`` — PIIDetector
- ``aigateway_core.shared.auth.key_store`` — KeyStore

The exception classes remain defined in ``aigateway_core.exceptions`` and are
re-exported here for backward compatibility.

See Task 3 of the runtime structure refactor.
"""
from __future__ import annotations

from .exceptions import AuthError, CircuitBreakerOpenError, GatewayError, QuotaExceededError
from aigateway_core.prefix.pii.detector import PIIDetector
from aigateway_core.shared.auth.key_store import KeyStore

# Re-export for backward compatibility (existing code imports from security.py)
__all__ = [
    "GatewayError",
    "AuthError",
    "QuotaExceededError",
    "CircuitBreakerOpenError",
    "KeyStore",
    "PIIDetector",
]
