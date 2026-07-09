"""Draft-to-HiRes generation - part of the generation pipeline.

Re-exports the strategy + plugin modules that live in this package.
"""
from . import draft_generator as _strategy
from . import draft_generator_plugin as _plugin

_sources = (_strategy, _plugin)
_names: list[str] = []
for _src in _sources:
    for _name in dir(_src):
        if _name.startswith("_"):
            continue
        if _name not in globals():
            globals()[_name] = getattr(_src, _name)
            _names.append(_name)

__all__ = _names
del _strategy, _plugin, _sources, _names, _src, _name
