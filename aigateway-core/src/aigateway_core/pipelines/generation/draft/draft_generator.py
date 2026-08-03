"""Public Draft generator strategy with explicit storage configuration."""
from __future__ import annotations

from typing import Any

from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError

from . import _draft_generator_impl as _impl

_CONFIGURATION_ERROR = (
    "config_missing:generation_optimization.draft_workflow.store_dir"
)
_GPU_TOPOLOGY_ERROR = "gpu_scheduler_topology_unavailable"


class DraftGeneratorStrategy(_impl.DraftGeneratorStrategy):
    """Draft strategy that never invents deployment storage or GPU topology."""

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

    @staticmethod
    def _pool_has_worker(status: Any, capability: str) -> bool:
        """Return whether the scheduler has a structurally valid worker pair."""
        if not isinstance(status, dict):
            return False
        devices = status.get("devices") or []
        workers = status.get("workers") or []
        if not isinstance(devices, list) or not isinstance(workers, list):
            return False
        device_uuids = {
            str(device.get("uuid"))
            for device in devices
            if isinstance(device, dict) and device.get("uuid")
        }
        for worker in workers:
            if not isinstance(worker, dict):
                continue
            if str(worker.get("device_uuid") or "") not in device_uuids:
                continue
            capabilities = worker.get("capabilities") or []
            if not isinstance(capabilities, list) or capability in capabilities:
                return True
        return False

    async def _run_on_comfy_worker(
        self,
        draft_id: str,
        capability: str,
        operation: Any,
        *,
        preferred_worker_id: str | None = None,
        memory_requirement_gb: float = 0.0,
    ) -> tuple[Any, Any | None]:
        """Keep enabled CUDA deployments inside the shared GPU pool.

        ``GpuResourceCoordinator`` arbitrates the same physical devices between
        Gateway model leases and ComfyUI generation workers.  When that pool is
        enabled, an empty or mismatched topology is a deployment error: directly
        calling the legacy ComfyUI URL would bypass generation priority, Gateway
        lease draining, Redis fencing, device locks and worker failover.

        The legacy direct URL remains valid only when the coordinator is absent
        or explicitly disabled; the base implementation already handles that
        compatibility mode.
        """
        coordinator = self._gpu_coordinator
        if coordinator is not None and coordinator.config.enabled:
            if not self._pool_has_worker(coordinator.status(), capability):
                raise DraftWorkflowError(_GPU_TOPOLOGY_ERROR)

        return await super()._run_on_comfy_worker(
            draft_id,
            capability,
            operation,
            preferred_worker_id=preferred_worker_id,
            memory_requirement_gb=memory_requirement_gb,
        )

    def _public_comfyui_error_code(
        self,
        exc: BaseException,
        *,
        fallback: str,
    ) -> str:
        if _GPU_TOPOLOGY_ERROR in str(exc).lower():
            return _GPU_TOPOLOGY_ERROR
        return super()._public_comfyui_error_code(exc, fallback=fallback)


for _name in dir(_impl):
    if _name.startswith("_") or _name == "DraftGeneratorStrategy":
        continue
    if _name not in globals():
        globals()[_name] = getattr(_impl, _name)

__all__ = tuple(name for name in globals() if not name.startswith("_"))
