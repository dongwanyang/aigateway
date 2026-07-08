"""Cost tracking + savings metrics + api-key groups — part of generation pipeline."""
from aigateway_core.generation_optimization import (
    metrics as _metrics,
    models as _models,
    api_key_groups as _keys,
)
from aigateway_core.generation_optimization.plugins import cost_tracker_plugin as _plugin

_sources = (_metrics, _models, _keys, _plugin)
_names: list[str] = []
for _src in _sources:
    for _name in dir(_src):
        if _name.startswith("_"):
            continue
        if _name not in globals():
            globals()[_name] = getattr(_src, _name)
            _names.append(_name)

__all__ = _names
del _metrics, _models, _keys, _plugin, _sources, _names, _src, _name
