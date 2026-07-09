"""Conversation compression - part of the understanding pipeline.

Authoritative implementation: ``aigateway_core.pipelines.understanding.conversation.conv_compressor_plugin``.
"""
from . import conv_compressor_plugin as _wrapped

_names: list[str] = []
for _name in dir(_wrapped):
    if _name.startswith("_"):
        continue
    if _name not in globals():
        globals()[_name] = getattr(_wrapped, _name)
        _names.append(_name)

__all__ = _names
del _wrapped, _names, _name
