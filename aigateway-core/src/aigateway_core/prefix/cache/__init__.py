"""L1/L2/L3 cache orchestration — part of the shared prefix layer (总 1).

Authoritative implementations:
- ``aigateway_core.prefix.cache.cache_keys`` — key/normalize helpers (lightweight)
- ``aigateway_core.prefix.cache.cache_manager`` — CacheManager, scheduler, rerankers
- ``aigateway_core.prefix.cache.plugin`` — PromptCachePlugin, SemanticCachePlugin
"""
from __future__ import annotations

# Eagerly import only the lightweight key helpers (no heavy deps, no circular risk)
from aigateway_core.prefix.cache.cache_keys import (  # noqa: F401
    _MAX_TOKENS_BUCKETS,
    _MODEL_SNAPSHOT_RE,
    _TEMPERATURE_BUCKETS,
    _bucket_max_tokens,
    _bucket_temperature,
    _model_family,
    _normalize_prompt,
)

__all__ = [
    "_MAX_TOKENS_BUCKETS",
    "_MODEL_SNAPSHOT_RE",
    "_TEMPERATURE_BUCKETS",
    "_bucket_max_tokens",
    "_bucket_temperature",
    "_model_family",
    "_normalize_prompt",
]
