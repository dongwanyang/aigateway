"""Draft-to-HiRes generation - part of the generation pipeline.

Re-exports the strategy + plugin modules that live in this package.
"""
from functools import wraps

from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError

from . import draft_generator as _strategy
from . import draft_generator_plugin as _plugin

_original_strategy_init = _strategy.DraftGeneratorStrategy.__init__


@wraps(_original_strategy_init)
def _configured_strategy_init(
    self,
    config,
    redis_client=None,
    comfyui_config=None,
    store_dir=None,
    task_tracker=None,
):
    effective_store_dir = store_dir if store_dir is not None else getattr(config, "store_dir", "")
    if not isinstance(effective_store_dir, str) or not effective_store_dir.strip():
        raise DraftWorkflowError(
            "config_missing:generation_optimization.draft_workflow.store_dir"
        )
    _original_strategy_init(
        self,
        config,
        redis_client=redis_client,
        comfyui_config=comfyui_config,
        store_dir=effective_store_dir.strip(),
        task_tracker=task_tracker,
    )


_strategy.DraftGeneratorStrategy.__init__ = _configured_strategy_init

_sources = (_strategy, _plugin)
_names: list[str] = []
for _src in _sources:
    for _name in dir(_src):
        if _name.startswith("_"):
            continue
        if _name not in globals():
            globals()[_name] = getattr(_src, _name)
            _names.append(_name)

__all__ = tuple(_names)
del _strategy, _plugin, _sources, _names, _src, _name
