"""LiteLLM bridge — part of the unified route layer.

Authoritative implementation: ``aigateway_core.litellm_bridge``.
"""
from aigateway_core import litellm_bridge as _wrapped

_public = [name for name in dir(_wrapped) if not name.startswith("_")]
for _name in _public:
    globals()[_name] = getattr(_wrapped, _name)

__all__ = _public
del _wrapped, _public, _name
