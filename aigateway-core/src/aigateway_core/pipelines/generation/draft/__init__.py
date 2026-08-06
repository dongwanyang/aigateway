"""Draft-to-HiRes generation utilities."""

from . import draft_generator as _strategy
from . import draft_generator_plugin as _plugin
from .cancellation_compat import install_pr47_cancellation_contract
from .terminal_task_visibility import install_terminal_task_visibility

install_terminal_task_visibility()
install_pr47_cancellation_contract()

_sources = (_strategy, _plugin)
_names: list[str] = []
for _source in _sources:
    for _name in dir(_source):
        if _name.startswith("_"):
            continue
        if _name not in globals():
            globals()[_name] = getattr(_source, _name)
            _names.append(_name)

__all__ = tuple(_names)
del _strategy, _plugin, _sources, _names, _source, _name
