"""Public Draft generator strategy with explicit storage configuration."""
from __future__ import annotations

from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError

from . import _draft_generator_impl as _impl

_CONFIGURATION_ERROR = (
    "config_missing:generation_optimization.draft_workflow.store_dir"
)


class DraftGeneratorStrategy(_impl.DraftGeneratorStrategy):
    """Draft strategy that never invents a deployment storage path."""

    def __init__(
        self,
        config,
        redis_client=None,
        comfyui_config=None,
        store_dir=None,
        task_tracker=None,
    ):
        store_dir_was_resolved_by_caller = store_dir is not None
        effective_store_dir = (
            store_dir
            if store_dir_was_resolved_by_caller
            else getattr(config, "store_dir", "")
        )
        configured = isinstance(effective_store_dir, str) and bool(
            effective_store_dir.strip()
        )
        if not configured and not store_dir_was_resolved_by_caller:
            raise DraftWorkflowError(_CONFIGURATION_ERROR)

        normalized_store_dir = effective_store_dir.strip() if configured else ""
        super().__init__(
            config,
            redis_client=redis_client,
            comfyui_config=comfyui_config,
            store_dir=normalized_store_dir,
            task_tracker=task_tracker,
        )
        self._configuration_error = None if configured else _CONFIGURATION_ERROR

    async def check_local_dependencies(self, *args, **kwargs):
        if self._configuration_error:
            raise DraftWorkflowError(self._configuration_error)
        return await super().check_local_dependencies(*args, **kwargs)


for _name in dir(_impl):
    if _name.startswith("_") or _name == "DraftGeneratorStrategy":
        continue
    if _name not in globals():
        globals()[_name] = getattr(_impl, _name)

__all__ = tuple(name for name in globals() if not name.startswith("_"))
