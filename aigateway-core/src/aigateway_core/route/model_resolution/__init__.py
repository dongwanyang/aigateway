"""Auto model resolution - part of the unified route layer.

Authoritative implementation:
``aigateway_core.route.model_resolution.model_router``.
"""
from . import model_router as _wrapped

_names: list[str] = []
for _name in dir(_wrapped):
    if _name.startswith("_"):
        continue
    if _name not in globals():
        globals()[_name] = getattr(_wrapped, _name)
        _names.append(_name)

__all__ = _names
del _wrapped, _names, _name
