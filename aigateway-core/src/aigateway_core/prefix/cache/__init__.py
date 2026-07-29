"""L1/L2/L3 cache orchestration — part of the shared prefix layer (总 1).

Authoritative implementations:
- ``aigateway_core.prefix.cache.cache_keys`` — key/normalize helpers (lightweight)
- ``aigateway_core.prefix.cache.cache_manager`` — CacheManager, scheduler, rerankers
- ``aigateway_core.prefix.cache.plugin`` — PromptCachePlugin, SemanticCachePlugin
"""
from __future__ import annotations

# Eagerly import only lightweight cache helpers and the L2 namespace adapter.
from aigateway_core.prefix.cache.cache_keys import (
    _MAX_TOKENS_BUCKETS,
    _MODEL_SNAPSHOT_RE,
    _TEMPERATURE_BUCKETS,
    _bucket_max_tokens,
    _bucket_temperature,
    _model_family,
    _normalize_prompt,
)
from aigateway_core.shared.runtime_values import redis_key_prefix

# ``l2_search`` historically exposed module constants used throughout its
# implementation. Set those constants from config before callers import/use the
# submodule, preserving its public API while removing deployment-specific names.
from aigateway_core.prefix.cache import l2_search as _l2_search

_l2_search.L2_INDEX_NAME = redis_key_prefix("l2_index")
_l2_search.L2_HASH_PREFIX = redis_key_prefix("l2_hash") + ":"

__all__ = [
    "_MAX_TOKENS_BUCKETS",
    "_MODEL_SNAPSHOT_RE",
    "_TEMPERATURE_BUCKETS",
    "_bucket_max_tokens",
    "_bucket_temperature",
    "_model_family",
    "_normalize_prompt",
]

del _l2_search
