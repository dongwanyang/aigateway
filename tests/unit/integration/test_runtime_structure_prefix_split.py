"""Runtime structure Task 3: prefix cache and security split.

Verifies that CacheManager, cache-key helpers, PIIDetector, and KeyStore
resolve from their new runtime-layer homes under ``aigateway_core.prefix``
and ``aigateway_core.shared``.
"""
from aigateway_core.prefix.cache.cache_keys import _normalize_prompt
from aigateway_core.prefix.cache.cache_manager import CacheManager
from aigateway_core.prefix.pii.detector import PIIDetector
from aigateway_core.shared.auth.key_store import KeyStore


def test_prefix_cache_and_security_modules_resolve_from_new_paths():
    assert _normalize_prompt("a   b") == "a b"
    assert PIIDetector.__name__ == "PIIDetector"
    assert KeyStore.__name__ == "KeyStore"
    assert CacheManager.__name__ == "CacheManager"
