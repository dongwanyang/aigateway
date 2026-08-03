"""Public GPU scheduler surface with strict worker-topology loading.

The coordinator implementation remains in ``_gpu_scheduler_impl``. This facade
keeps the established import path while enforcing one ownership rule at the
configuration boundary: an enabled local scheduler may only receive explicitly
generated worker/device UUID mappings. The legacy single-URL fallback is reserved
for deployments where the scheduler is absent or disabled.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import _gpu_scheduler_impl as _impl

# Preserve public and test-facing attributes from the implementation module.
for _name in dir(_impl):
    if _name.startswith("__") or _name == "workers_from_config":
        continue
    globals()[_name] = getattr(_impl, _name)


def _explicit_workers(config: Mapping[str, Any]) -> list[ComfyWorker]:
    scheduler = config.get("gpu_scheduler", {})
    raw_workers = (
        scheduler.get("workers", [])
        if isinstance(scheduler, Mapping)
        else []
    )
    workers: list[ComfyWorker] = []
    if not isinstance(raw_workers, list):
        return workers
    for index, raw in enumerate(raw_workers):
        if not isinstance(raw, Mapping):
            continue
        device_uuid = str(raw.get("device_uuid") or "")
        server_url = str(raw.get("server_url") or "").rstrip("/")
        if not device_uuid or not server_url:
            continue
        capabilities = raw.get(
            "capabilities",
            ["image", "video", "upscale"],
        )
        if not isinstance(capabilities, list):
            capabilities = ["image", "video", "upscale"]
        workers.append(
            ComfyWorker(
                worker_id=str(raw.get("worker_id") or f"comfyui-{index}"),
                device_uuid=device_uuid,
                server_url=server_url,
                capabilities=frozenset(str(item) for item in capabilities if item),
            )
        )
    return workers


def workers_from_config(
    config: Mapping[str, Any],
    devices: Sequence[GpuDevice],
) -> list[ComfyWorker]:
    """Load explicit pool workers and isolate external ComfyUI endpoints.

    When ``gpu_scheduler.enabled`` is true, a fixed ComfyUI URL is not sufficient
    evidence that the endpoint owns any local GPU. Only generated worker entries
    with stable ``device_uuid`` values may enter ``GpuResourceCoordinator``.
    This prevents remote ComfyUI telemetry, queues and jobs from draining or
    locking local Gateway devices.

    The historical single-URL fallback remains available only when the scheduler
    is absent or explicitly disabled.
    """
    workers = _explicit_workers(config)
    scheduler = config.get("gpu_scheduler", {})
    scheduler_enabled = (
        isinstance(scheduler, Mapping)
        and scheduler.get("enabled") is True
    )
    if workers or not devices or scheduler_enabled:
        return workers
    return _impl.workers_from_config(config, devices)


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


__all__ = tuple(
    name
    for name in _impl.__all__
    if name != "workers_from_config"
) + ("workers_from_config",)
