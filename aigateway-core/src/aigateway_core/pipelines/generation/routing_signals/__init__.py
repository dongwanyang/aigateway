"""Generation-side model routing signal — feeds the route/ layer.

The final model resolution decision lives in the ``route/`` layer; this
subpackage exposes the generation-specific plugin that emits the routing
signal.
"""
from aigateway_core.generation_optimization.plugins import gen_model_router_plugin as _wrapped

_public = [name for name in dir(_wrapped) if not name.startswith("_")]
for _name in _public:
    globals()[_name] = getattr(_wrapped, _name)

__all__ = _public
del _wrapped, _public, _name
