"""L1/L2/L3 cache orchestration — part of the shared prefix layer (总 1).

Authoritative implementations:
- ``aigateway_core.prefix.cache.cache_keys`` — key/normalize helpers (lightweight)
- ``aigateway_core.prefix.cache.cache_manager`` — CacheManager, scheduler, rerankers
- ``aigateway_core.prefix.cache.plugin`` — PromptCachePlugin, SemanticCachePlugin
"""
from __future__ import annotations

from functools import wraps
from typing import Any

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

# ``l2_search`` historically uses module constants internally. Wrap its three
# public Redis operations so those constants are refreshed from config immediately
# before use, without making package import depend on the current working directory.
from aigateway_core.prefix.cache import l2_search as _l2_search


def _configure_l2_namespace() -> None:
    _l2_search.L2_INDEX_NAME = redis_key_prefix("l2_index")
    _l2_search.L2_HASH_PREFIX = redis_key_prefix("l2_hash") + ":"


def _wrap_l2_operation(operation):
    @wraps(operation)
    async def wrapped(*args: Any, **kwargs: Any):
        _configure_l2_namespace()
        return await operation(*args, **kwargs)

    return wrapped


_l2_search.ensure_index = _wrap_l2_operation(_l2_search.ensure_index)
_l2_search.store = _wrap_l2_operation(_l2_search.store)
_l2_search.search = _wrap_l2_operation(_l2_search.search)

__all__ = [
    "_MAX_TOKENS_BUCKETS",
    "_MODEL_SNAPSHOT_RE",
    "_TEMPERATURE_BUCKETS",
    "_bucket_max_tokens",
    "_bucket_temperature",
    "_model_family",
    "_normalize_prompt",
]
