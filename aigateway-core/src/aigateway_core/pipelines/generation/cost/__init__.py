"""Cost tracking plugin - part of the generation pipeline.

Re-exports the cost-tracker plugin module that lives in this package.
Shared cost models / metrics / api-key groups live in
``aigateway_core.pipelines.generation._common``.
"""
from . import cost_tracker_plugin as _plugin

_names: list[str] = []
for _name in dir(_plugin):
    if _name.startswith("_"):
        continue
    if _name not in globals():
        globals()[_name] = getattr(_plugin, _name)
        _names.append(_name)

__all__ = _names
del _plugin, _names, _name
