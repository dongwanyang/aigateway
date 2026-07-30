"""Public L2 BM25 module with config-backed Redis namespace resolution."""
from __future__ import annotations

from typing import Any

from aigateway_core.shared.runtime_values import redis_key_prefix

from . import _l2_search_impl as _impl

L2_INDEX_NAME = ""
L2_HASH_PREFIX = ""


def _configure_namespace() -> None:
    global L2_INDEX_NAME, L2_HASH_PREFIX
    L2_INDEX_NAME = redis_key_prefix("l2_index")
    L2_HASH_PREFIX = redis_key_prefix("l2_hash") + ":"
    _impl.L2_INDEX_NAME = L2_INDEX_NAME
    _impl.L2_HASH_PREFIX = L2_HASH_PREFIX


async def ensure_index(*args: Any, **kwargs: Any):
    _configure_namespace()
    return await _impl.ensure_index(*args, **kwargs)


async def store(*args: Any, **kwargs: Any):
    _configure_namespace()
    return await _impl.store(*args, **kwargs)


async def search(*args: Any, **kwargs: Any):
    _configure_namespace()
    return await _impl.search(*args, **kwargs)


for _name in dir(_impl):
    if _name.startswith("_") or _name in {"ensure_index", "store", "search"}:
        continue
    if _name not in globals():
        globals()[_name] = getattr(_impl, _name)

__all__ = tuple(
    name for name in globals() if not name.startswith("_")
)
