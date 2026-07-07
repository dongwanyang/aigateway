"""L1/L2/L3 cache orchestration — part of the shared prefix layer.

Authoritative implementations live in ``aigateway_core.caching`` and
``aigateway_core.pipeline.{PromptCachePlugin,SemanticCachePlugin}``.
"""
from aigateway_core import caching as _caching
from aigateway_core.pipeline import PromptCachePlugin, SemanticCachePlugin

_public = [name for name in dir(_caching) if not name.startswith("_")]
for _name in _public:
    globals()[_name] = getattr(_caching, _name)

__all__ = _public + ["PromptCachePlugin", "SemanticCachePlugin"]
del _caching, _public, _name
