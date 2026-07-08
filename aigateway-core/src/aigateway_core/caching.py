"""Compatibility shim — real implementations moved to ``aigateway_core.prefix.cache``.

This module re-exports the public names previously defined here so that
existing callers (``from aigateway_core.caching import CacheManager`` etc.)
keep working. New code should import from the owning modules:

- ``aigateway_core.prefix.cache.cache_keys`` — key/normalize helpers
- ``aigateway_core.prefix.cache.cache_manager`` — CacheManager, scheduler, rerankers

See Task 3 of the runtime structure refactor.
"""
from __future__ import annotations

from aigateway_core.prefix.cache.cache_keys import (
    _MAX_TOKENS_BUCKETS,
    _MODEL_SNAPSHOT_RE,
    _TEMPERATURE_BUCKETS,
    _bucket_max_tokens,
    _bucket_temperature,
    _model_family,
    _normalize_prompt,
)
from aigateway_core.prefix.cache.cache_manager import (
    L1_MAX_VALUE_BYTES,
    L2_MAX_VALUE_BYTES,
    L3_CLEANUP_INTERVAL,
    L3_DEFAULT_TTL,
    L3_MIN_TOKEN_COUNT,
    L3CleanupScheduler,
    CacheManager,
    CrossEncoderReranker,
    LightweightReranker,
    SemanticCacheWithRerank,
    _emit_cache_debug,
)

__all__ = [
    "CacheManager",
    "L3CleanupScheduler",
    "LightweightReranker",
    "CrossEncoderReranker",
    "SemanticCacheWithRerank",
    "_emit_cache_debug",
    "_MODEL_SNAPSHOT_RE",
    "_TEMPERATURE_BUCKETS",
    "_MAX_TOKENS_BUCKETS",
    "_bucket_temperature",
    "_bucket_max_tokens",
    "_model_family",
    "_normalize_prompt",
    "L1_MAX_VALUE_BYTES",
    "L2_MAX_VALUE_BYTES",
    "L3_MIN_TOKEN_COUNT",
    "L3_DEFAULT_TTL",
    "L3_CLEANUP_INTERVAL",
]
