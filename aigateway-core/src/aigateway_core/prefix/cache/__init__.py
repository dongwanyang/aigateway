"""L1/L2/L3 cache orchestration — part of the shared prefix layer (总 1).

Authoritative implementations:
- ``aigateway_core.prefix.cache.cache_keys`` — key/normalize helpers (lightweight)
- ``aigateway_core.prefix.cache.cache_manager`` — CacheManager, scheduler, rerankers
- ``aigateway_core.pipeline.{PromptCachePlugin,SemanticCachePlugin}`` — pipeline plugins

``cache_keys`` symbols are imported eagerly (lightweight, no heavy deps).
``cache_manager`` symbols and pipeline plugins are exposed lazily via
``__getattr__``: ``cache_manager`` pulls in lz4/cachetools, and importing
``aigateway_core.pipeline`` during package init triggers a circular import
(pipeline → security shim → prefix.pii → this package). Import the owning
modules directly for the lightest path:
``from aigateway_core.prefix.cache.cache_keys import _normalize_prompt``.
"""
from __future__ import annotations

import importlib as _importlib

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

# Lazy: cache_manager symbols (lz4/cachetools) + pipeline plugin symbols (circular)
_LAZY_SOURCES = {
    "CacheManager": "aigateway_core.prefix.cache.cache_manager",
    "L3CleanupScheduler": "aigateway_core.prefix.cache.cache_manager",
    "LightweightReranker": "aigateway_core.prefix.cache.cache_manager",
    "CrossEncoderReranker": "aigateway_core.prefix.cache.cache_manager",
    "SemanticCacheWithRerank": "aigateway_core.prefix.cache.cache_manager",
    "_emit_cache_debug": "aigateway_core.prefix.cache.cache_manager",
    "L1_MAX_VALUE_BYTES": "aigateway_core.prefix.cache.cache_manager",
    "L2_MAX_VALUE_BYTES": "aigateway_core.prefix.cache.cache_manager",
    "L3_MIN_TOKEN_COUNT": "aigateway_core.prefix.cache.cache_manager",
    "L3_DEFAULT_TTL": "aigateway_core.prefix.cache.cache_manager",
    "L3_CLEANUP_INTERVAL": "aigateway_core.prefix.cache.cache_manager",
    "PromptCachePlugin": "aigateway_core.pipeline",
    "SemanticCachePlugin": "aigateway_core.pipeline",
}


def __getattr__(name: str):
    if name in _LAZY_SOURCES:
        mod = _importlib.import_module(_LAZY_SOURCES[name])
        value = getattr(mod, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    "PromptCachePlugin",
    "SemanticCachePlugin",
]
