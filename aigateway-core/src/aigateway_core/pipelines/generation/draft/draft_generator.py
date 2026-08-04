"""Public Draft generator strategy with explicit storage configuration."""
from __future__ import annotations

import os
import time
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

    async def submit_draft(
        self,
        request,
        config,
        keyframe_count=None,
        chat_session_id=None,
        user_id=None,
        group_id=None,
    ):
        """Persist the complete video plan before background generation runs."""
        if request.media_type == "video" and request.keyframe_prompt:
            request.prompt = request.keyframe_prompt
        draft = await super().submit_draft(
            request,
            config,
            keyframe_count,
            chat_session_id,
            user_id,
            group_id,
        )
        if request.media_type == "video":
            params = draft.generation_params
            params.update(
                {
                    "source_prompt": request.source_prompt or request.prompt,
                    "keyframe_prompt": request.keyframe_prompt or request.prompt,
                    "motion_prompt": request.motion_prompt or request.prompt,
                    "prompt_language": request.prompt_language,
                    "keyframe_language": request.keyframe_language,
                    "motion_language": request.motion_language,
                    "language_fallback_reason": request.language_fallback_reason,
                    "duration_seconds": request.duration_seconds,
                    "fps": request.target_fps,
                    "frame_count": request.frame_count,
                    "source_draft_id": request.source_draft_id,
                    "source_image_sha256": request.source_image_sha256,
                }
            )
            # ``generation_params`` is shared with the just-created background
            # task, so this update is visible before its first scheduling turn.
            await self._store_draft(
                draft,
                max(1, int(draft.expires_at - time.time())),
            )
        return draft

    async def _generate_video_with_comfyui(self, draft):
        """Use the persisted motion prompt; never reuse the static keyframe text."""
        if not self._comfyui_config.video_enabled:
            raise DraftWorkflowError("comfyui_video_not_enabled")
        if not draft.previews:
            raise DraftWorkflowError("Video draft has no approved keyframe")
        await self._ensure_storage_capacity()
        input_name = await self._upload_image(
            draft.previews[0], f"video-keyframe-{draft.draft_id}.png"
        )
        motion_prompt = str(
            draft.generation_params.get("motion_prompt")
            or draft.generation_params.get("prompt")
            or ""
        )
        workflow = self._build_video_workflow(
            input_name=input_name,
            prompt=motion_prompt,
            seed=int(draft.generation_params.get("seed", 0)),
            draft_id=draft.draft_id,
        )
        async with self._comfyui_semaphore:
            client_id = self._comfy_client_id(draft.draft_id, "video")
            prompt_id = await self._submit_workflow(workflow, client_id=client_id)
            await self._record_comfy_job(draft.draft_id, prompt_id, "refining")
            result = await self._poll_result(
                prompt_id,
                timeout=self._comfyui_config.video_execution_timeout,
                trace_id=str(draft.generation_params.get("trace_id") or ""),
                draft_id=draft.draft_id,
                progress_client_id=client_id,
                progress_stage="refining",
            )
        return result

    @staticmethod
    def _pool_has_worker(status: Any, capability: str) -> bool:
        """Return whether the scheduler has a valid worker/device capability pair."""
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
            if isinstance(capabilities, list) and capability in capabilities:
                return True
        return False

    def _scheduler_manages_comfyui(self, status: Any) -> bool:
        configured = bool(
            getattr(self._comfyui_config, "scheduler_managed", False)
        )
        shared_env = os.getenv("AIGATEWAY_SHARED_GPU", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        workers = status.get("workers") if isinstance(status, dict) else []
        return configured or shared_env or bool(workers)

    async def _run_on_comfy_worker(
        self,
        draft_id: str,
        capability: str,
        operation: Any,
        *,
        preferred_worker_id: str | None = None,
        memory_requirement_gb: float = 0.0,
    ) -> tuple[Any, Any | None]:
        coordinator = self._gpu_coordinator
        if coordinator is not None and coordinator.config.enabled:
            status = coordinator.status()
            if not self._scheduler_manages_comfyui(status):
                return await operation(), None
            if not self._pool_has_worker(status, capability):
                raise DraftWorkflowError(_GPU_TOPOLOGY_ERROR)

        return await super()._run_on_comfy_worker(
            draft_id,
            capability,
            operation,
            preferred_worker_id=preferred_worker_id,
            memory_requirement_gb=memory_requirement_gb,
        )

    async def _mark_draft_confirmation_failed(
        self,
        draft: Any,
        reason: str,
    ) -> None:
        draft.comfy_prompt_id = None
        generation_params = getattr(draft, "generation_params", None)
        if isinstance(generation_params, dict):
            generation_params["progress_source"] = "stage"
        await super()._mark_draft_confirmation_failed(draft, reason)

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
