"""Generation-side model routing signal - feeds the route/ layer.

The final model resolution decision lives in the ``route/`` layer; this
subpackage exposes the generation-specific plugin that emits the routing
signal.
"""
from . import gen_model_router_plugin as _wrapped

_names: list[str] = []
for _name in dir(_wrapped):
    if _name.startswith("_"):
        continue
    if _name not in globals():
        globals()[_name] = getattr(_wrapped, _name)
        _names.append(_name)

__all__ = _names
del _wrapped, _names, _name
