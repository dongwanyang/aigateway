"""Draft-to-HiRes generation - part of the generation pipeline.

Re-exports the strategy + plugin modules that live in this package.
"""
from functools import wraps

from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError

from . import draft_generator as _strategy
from . import draft_generator_plugin as _plugin

_CONFIGURATION_ERROR = "config_missing:generation_optimization.draft_workflow.store_dir"
_original_strategy_init = _strategy.DraftGeneratorStrategy.__init__
_original_check_local_dependencies = (
    _strategy.DraftGeneratorStrategy.check_local_dependencies
)


@wraps(_original_strategy_init)
def _configured_strategy_init(
    self,
    config,
    redis_client=None,
    comfyui_config=None,
    store_dir=None,
    task_tracker=None,
):
    """Initialize without inventing a deployment path.

    Registration is a public API and must remain usable without a ConfigManager.
    When no store directory is configured, construct an unavailable strategy and
    defer the explicit configuration error until the draft capability is actually
    exercised. This preserves the six-plugin dependency chain while preventing
    writes relative to the process working directory.
    """
    effective_store_dir = (
        store_dir if store_dir is not None else getattr(config, "store_dir", "")
    )
    configured = isinstance(effective_store_dir, str) and bool(
        effective_store_dir.strip()
    )
    normalized_store_dir = effective_store_dir.strip() if configured else ""

    _original_strategy_init(
        self,
        config,
        redis_client=redis_client,
        comfyui_config=comfyui_config,
        store_dir=normalized_store_dir,
        task_tracker=task_tracker,
    )
    self._configuration_error = None if configured else _CONFIGURATION_ERROR


@wraps(_original_check_local_dependencies)
async def _configured_check_local_dependencies(self, *args, **kwargs):
    configuration_error = getattr(self, "_configuration_error", None)
    if configuration_error:
        raise DraftWorkflowError(configuration_error)
    return await _original_check_local_dependencies(self, *args, **kwargs)


_strategy.DraftGeneratorStrategy.__init__ = _configured_strategy_init
_strategy.DraftGeneratorStrategy.check_local_dependencies = (
    _configured_check_local_dependencies
)

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
