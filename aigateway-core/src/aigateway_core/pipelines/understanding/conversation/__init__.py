"""Conversation compression — part of the understanding pipeline.

Authoritative implementation: ``aigateway_core.plugins.conv_compressor_plugin``.
"""
from aigateway_core.plugins import conv_compressor_plugin as _wrapped

_public = [name for name in dir(_wrapped) if not name.startswith("_")]
for _name in _public:
    globals()[_name] = getattr(_wrapped, _name)

__all__ = _public
del _wrapped, _public, _name
