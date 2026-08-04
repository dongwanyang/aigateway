"""Public Draft generator strategy with explicit storage configuration."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import os
import time
from typing import Any

from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError

from . import _draft_generator_impl as _impl

_CONFIGURATION_ERROR = (
    "config_missing:generation_optimization.draft_workflow.store_dir"
)
_GPU_TOPOLOGY_ERROR = "gpu_scheduler_topology_unavailable"
_VIDEO_ERROR_CODES = {
    "video_keyframe_integrity_missing",
    "video_keyframe_integrity_mismatch",
    "video_duration_unsupported",
    "video_fps_invalid",
    "comfyui_video_workflow_invalid",
}


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
        self._confirmation_task_lock = asyncio.Lock()
        self._confirmation_tasks: dict[str, asyncio.Task[Any]] = {}

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
                    # Never trust a client-provided digest as the frozen preview
                    # identity. It is calculated from the persisted preview below.
                    "requested_source_image_sha256": request.source_image_sha256,
                    "source_image_sha256": None,
                }
            )
            await self._store_draft(
                draft,
                max(1, int(draft.expires_at - time.time())),
            )
        return draft

    async def confirm_draft(self, draft_id: str):
        """Coalesce concurrent confirmation requests for the same draft.

        The persisted pending-to-refining compare-and-set remains the
        cross-process idempotency gate. This local registry additionally makes
        rapid duplicate requests on one API worker await the same task and
        receive the same result instead of racing into a state-conflict error.
        """
        async with self._confirmation_task_lock:
            task = self._confirmation_tasks.get(draft_id)
            if task is None or task.done():
                task = asyncio.create_task(
                    self._confirm_draft_impl(draft_id),
                    name=f"draft-confirm-{draft_id}",
                )
                self._confirmation_tasks[draft_id] = task
                self._bg_tasks.add(task)

                def _confirmation_done(completed: asyncio.Task[Any]) -> None:
                    self._bg_tasks.discard(completed)
                    current = self._confirmation_tasks.get(draft_id)
                    if current is completed:
                        self._confirmation_tasks.pop(draft_id, None)
                    try:
                        completed.exception()
                    except (Exception, asyncio.CancelledError):
                        pass

                task.add_done_callback(_confirmation_done)

        return await asyncio.shield(task)

    async def reject_draft(self, draft_id: str):
        """Prevent source-result video drafts from replacing their frozen image."""
        draft = await self.get_draft(draft_id)
        if (
            draft is not None
            and draft.media_type == "video"
            and str(draft.generation_params.get("source_kind") or "")
            == "draft_result"
        ):
            raise DraftWorkflowError("source_draft_immutable")
        return await super().reject_draft(draft_id)

    @staticmethod
    def _source_kind(draft: Any) -> str:
        params = draft.generation_params
        return (
            "source_draft"
            if params.get("source_draft_id")
            else "uploaded"
            if params.get("has_reference_image")
            else "generated_keyframe"
        )

    @classmethod
    def _freeze_video_keyframe(cls, draft: Any) -> None:
        """Freeze the exact preview digest once for each video draft identity.

        A confirmation failure may return the same draft to ``pending``. That
        rollback must never re-baseline changed preview bytes. Regeneration has
        a new draft ID, so it receives a new digest even though its generation
        parameters were copied from the rejected draft.
        """
        if (
            draft.media_type != "video"
            or draft.status != _impl.DRAFT_STATUS_PENDING
            or not draft.previews
        ):
            return

        params = draft.generation_params
        expected_hash = str(params.get("source_image_sha256") or "")
        frozen_draft_id = str(params.get("source_image_frozen_draft_id") or "")

        if expected_hash and (
            not frozen_draft_id or frozen_draft_id == draft.draft_id
        ):
            params["source_image_frozen_draft_id"] = draft.draft_id
            params.setdefault("source_kind", cls._source_kind(draft))
            return

        params["source_image_sha256"] = hashlib.sha256(
            draft.previews[0]
        ).hexdigest()
        params["source_image_frozen_draft_id"] = draft.draft_id
        params["source_kind"] = cls._source_kind(draft)

    @staticmethod
    def _validate_frozen_video_keyframe(draft: Any) -> None:
        """Validate frozen video input before acquiring a generation worker."""
        if draft.media_type != "video":
            return
        if not draft.previews:
            raise DraftWorkflowError("video_keyframe_integrity_missing")

        params = draft.generation_params
        expected_hash = str(params.get("source_image_sha256") or "")
        if not expected_hash:
            raise DraftWorkflowError("video_keyframe_integrity_missing")

        frozen_draft_id = str(params.get("source_image_frozen_draft_id") or "")
        if frozen_draft_id and frozen_draft_id != draft.draft_id:
            raise DraftWorkflowError("video_keyframe_integrity_mismatch")

        actual_hash = hashlib.sha256(draft.previews[0]).hexdigest()
        if not hmac.compare_digest(actual_hash, expected_hash):
            raise DraftWorkflowError("video_keyframe_integrity_mismatch")

    async def _store_draft(self, draft, ttl_seconds):
        self._freeze_video_keyframe(draft)
        await super()._store_draft(draft, ttl_seconds)

    async def _regenerate_draft(self, old_draft):
        """Preserve the old draft identity before copying frozen parameters."""
        if old_draft.media_type == "video":
            params = old_draft.generation_params
            if params.get("source_image_sha256") and not params.get(
                "source_image_frozen_draft_id"
            ):
                params["source_image_frozen_draft_id"] = old_draft.draft_id
        return await super()._regenerate_draft(old_draft)

    async def _claim_draft_confirmation(self, draft_id: str):
        """Claim and validate frozen video input before worker scheduling."""
        draft, claimed = await super()._claim_draft_confirmation(draft_id)
        if not claimed or draft is None or draft.media_type != "video":
            return draft, claimed

        try:
            self._validate_frozen_video_keyframe(draft)
        except DraftWorkflowError as exc:
            await self._mark_draft_confirmation_failed(draft, str(exc))
            raise
        return draft, claimed

    async def _generate_video_with_comfyui(self, draft):
        """Generate Wan video from the frozen keyframe and motion-only prompt."""
        if not self._comfyui_config.video_enabled:
            raise DraftWorkflowError("comfyui_video_not_enabled")
        self._validate_frozen_video_keyframe(draft)

        frame_count = self._video_frame_count(draft)
        fps = self._video_fps(draft)
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
        self._apply_video_timing(
            workflow,
            frame_count=frame_count,
            fps=fps,
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
    def _video_frame_count(draft: Any) -> int:
        value = draft.generation_params.get("frame_count")
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            or value % 4 != 1
        ):
            raise DraftWorkflowError("video_duration_unsupported")
        return value

    @staticmethod
    def _video_fps(draft: Any) -> float:
        value = draft.generation_params.get("fps")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
        ):
            raise DraftWorkflowError("video_fps_invalid")
        return float(value)

    @staticmethod
    def _apply_video_timing(
        workflow: dict[str, Any],
        *,
        frame_count: int,
        fps: float,
    ) -> None:
        try:
            workflow["5"]["inputs"]["length"] = frame_count
            workflow["11"]["inputs"]["fps"] = fps
        except (KeyError, TypeError) as exc:
            raise DraftWorkflowError("comfyui_video_workflow_invalid") from exc

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
        error_text = str(exc).lower()
        if _GPU_TOPOLOGY_ERROR in error_text:
            return _GPU_TOPOLOGY_ERROR
        for code in _VIDEO_ERROR_CODES:
            if code in error_text:
                return code
        return super()._public_comfyui_error_code(exc, fallback=fallback)


for _name in dir(_impl):
    if _name.startswith("_") or _name == "DraftGeneratorStrategy":
        continue
    if _name not in globals():
        globals()[_name] = getattr(_impl, _name)

__all__ = tuple(name for name in globals() if not name.startswith("_"))
