"""Public L2 BM25 module with config-backed Redis namespace resolution."""
from __future__ import annotations

from typing import Any

from aigateway_core.shared.runtime_values import redis_key_prefix

from . import _l2_search_impl as _impl

# Exposed for diagnostics and compatibility. Operations use local resolved values
# and pass them explicitly to the implementation, so concurrent calls never depend
# on these mutable module attributes.
L2_INDEX_NAME = ""
L2_HASH_PREFIX = ""


def _configured_namespace() -> tuple[str, str]:
    global L2_INDEX_NAME, L2_HASH_PREFIX
    index_name = redis_key_prefix("l2_index")
    hash_prefix = redis_key_prefix("l2_hash") + ":"
    L2_INDEX_NAME = index_name
    L2_HASH_PREFIX = hash_prefix
    return index_name, hash_prefix


async def ensure_index(*args: Any, **kwargs: Any):
    index_name, hash_prefix = _configured_namespace()
    kwargs.setdefault("index_name", index_name)
    kwargs.setdefault("hash_prefix", hash_prefix)
    return await _impl.ensure_index(*args, **kwargs)


async def store(*args: Any, **kwargs: Any):
    _, hash_prefix = _configured_namespace()
    kwargs.setdefault("hash_prefix", hash_prefix)
    return await _impl.store(*args, **kwargs)


async def search(*args: Any, **kwargs: Any):
    index_name, _ = _configured_namespace()
    kwargs.setdefault("index_name", index_name)
    return await _impl.search(*args, **kwargs)


# Re-export the former module surface, including private test helpers and schema
# constants. The wrapped operations and configured names remain authoritative.
for _name in dir(_impl):
    if _name.startswith("__") or _name in {
        "ensure_index",
        "store",
        "search",
        "L2_INDEX_NAME",
        "L2_HASH_PREFIX",
    }:
        continue
    if _name not in globals():
        globals()[_name] = getattr(_impl, _name)

__all__ = tuple(name for name in globals() if not name.startswith("__"))
