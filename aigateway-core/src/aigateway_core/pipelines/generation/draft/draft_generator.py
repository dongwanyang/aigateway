"""Public Draft generator strategy with explicit storage configuration."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import time
from typing import Any

from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation._common.models import (
    DRAFT_STATUS_CANCELLED,
    DRAFT_STATUS_COMPLETED,
    DRAFT_STATUS_CONFIRMED,
    DRAFT_STATUS_EXPIRED,
    DRAFT_STATUS_FAILED,
    DRAFT_STATUS_REJECTED,
)

from . import _draft_generator_impl as _impl

_CONFIGURATION_ERROR = (
    "config_missing:generation_optimization.draft_workflow.store_dir"
)
_GPU_TOPOLOGY_ERROR = "gpu_scheduler_topology_unavailable"
_REQUEST_INDEX_PREFIX = "aigateway:draft:request"
_REQUEST_CANCEL_PREFIX = "aigateway:draft:cancel"
_REQUEST_CANCEL_TTL_SECONDS = 300
_VIDEO_ERROR_CODES = {
    "video_keyframe_integrity_missing",
    "video_keyframe_integrity_mismatch",
    "video_duration_unsupported",
    "video_fps_invalid",
    "comfyui_video_workflow_invalid",
}
_TERMINAL_DRAFT_STATUSES = {
    DRAFT_STATUS_COMPLETED,
    DRAFT_STATUS_CONFIRMED,
    DRAFT_STATUS_REJECTED,
    DRAFT_STATUS_EXPIRED,
    DRAFT_STATUS_FAILED,
    DRAFT_STATUS_CANCELLED,
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
        self._draft_tasks: dict[str, asyncio.Task[Any]] = {}

    def _redis_connection(self) -> Any:
        return getattr(self._redis_client, "redis", self._redis_client)

    async def _set_runtime_value(self, key: str, value: str, ttl_seconds: int) -> None:
        connection = self._redis_connection()
        if connection is not None:
            await connection.set(key, value, ex=max(1, int(ttl_seconds)))
        else:
            self._memory_store[key] = value

    async def _get_runtime_value(self, key: str) -> str | None:
        connection = self._redis_connection()
        raw = await connection.get(key) if connection is not None else self._memory_store.get(key)
        if raw is None:
            return None
        return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

    @staticmethod
    def _request_record(
        *,
        draft_id: str | None,
        user_id: str | None,
        group_id: str | None,
        session_id: str | None,
    ) -> dict[str, str | None]:
        return {
            "draft_id": draft_id,
            "user_id": user_id,
            "group_id": group_id,
            "session_id": session_id,
        }

    @staticmethod
    def _record_matches_owner(
        record: dict[str, Any],
        *,
        user_id: str | None,
        group_id: str | None,
        session_id: str | None,
    ) -> bool:
        expected_user = str(record.get("user_id") or "")
        expected_group = str(record.get("group_id") or "")
        expected_session = str(record.get("session_id") or "")
        if expected_user and expected_user != str(user_id or ""):
            return False
        if expected_group and expected_group != str(group_id or ""):
            return False
        if expected_session and expected_session != str(session_id or ""):
            return False
        return bool(expected_user or expected_group)

    async def register_request_draft(
        self,
        request_id: str,
        draft_id: str,
        *,
        user_id: str | None,
        group_id: str | None,
        session_id: str | None,
        ttl_seconds: int,
    ) -> None:
        request_id = str(request_id or "").strip()
        if not request_id:
            return
        record = self._request_record(
            draft_id=draft_id,
            user_id=user_id,
            group_id=group_id,
            session_id=session_id,
        )
        await self._set_runtime_value(
            f"{_REQUEST_INDEX_PREFIX}:{request_id}",
            json.dumps(record, separators=(",", ":")),
            ttl_seconds,
        )

    async def resolve_request(
        self,
        request_id: str,
    ) -> tuple[Any | None, dict[str, Any] | None]:
        request_id = str(request_id or "").strip()
        if not request_id:
            return None, None
        raw = await self._get_runtime_value(f"{_REQUEST_INDEX_PREFIX}:{request_id}")
        if not raw:
            return None, None
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            return None, None
        if not isinstance(record, dict):
            return None, None
        draft_id = str(record.get("draft_id") or "")
        draft = await self.get_draft(draft_id) if draft_id else None
        return draft, record

    async def _cancel_record(self, request_id: str) -> dict[str, Any] | None:
        raw = await self._get_runtime_value(f"{_REQUEST_CANCEL_PREFIX}:{request_id}")
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    async def _set_cancel_record(
        self,
        request_id: str,
        *,
        user_id: str | None,
        group_id: str | None,
        session_id: str | None,
        ttl_seconds: int = _REQUEST_CANCEL_TTL_SECONDS,
    ) -> None:
        if not request_id:
            return
        record = self._request_record(
            draft_id=None,
            user_id=user_id,
            group_id=group_id,
            session_id=session_id,
        )
        record["cancelled_at"] = time.time()
        await self._set_runtime_value(
            f"{_REQUEST_CANCEL_PREFIX}:{request_id}",
            json.dumps(record, separators=(",", ":")),
            ttl_seconds,
        )

    async def _draft_cancel_requested(self, draft_id: str) -> bool:
        draft = await self.get_draft(draft_id)
        if draft is None:
            return False
        request_id = str(draft.generation_params.get("request_id") or "")
        if not request_id:
            return False
        record = await self._cancel_record(request_id)
        if record is None:
            return False
        return self._record_matches_owner(
            record,
            user_id=draft.user_id,
            group_id=draft.group_id,
            session_id=draft.session_id,
        )

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
        """Persist the complete plan and register a recoverable request identity."""
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
                    "requested_source_image_sha256": request.source_image_sha256,
                    "source_image_sha256": None,
                }
            )
            await self._store_draft(
                draft,
                max(1, int(draft.expires_at - time.time())),
            )

        ttl_seconds = max(1, int(draft.expires_at - time.time()))
        await self.register_request_draft(
            request.request_id,
            draft.draft_id,
            user_id=user_id,
            group_id=group_id,
            session_id=chat_session_id,
            ttl_seconds=ttl_seconds,
        )
        task_name = f"draft-generate-{draft.draft_id}"
        task = next(
            (
                candidate
                for candidate in self._bg_tasks
                if not candidate.done() and candidate.get_name() == task_name
            ),
            None,
        )
        if task is not None:
            self._draft_tasks[draft.draft_id] = task

            def _draft_done(completed: asyncio.Task[Any]) -> None:
                current = self._draft_tasks.get(draft.draft_id)
                if current is completed:
                    self._draft_tasks.pop(draft.draft_id, None)

            task.add_done_callback(_draft_done)

        cancel_record = await self._cancel_record(str(request.request_id or ""))
        if cancel_record and self._record_matches_owner(
            cancel_record,
            user_id=user_id,
            group_id=group_id,
            session_id=chat_session_id,
        ):
            await self.cancel_draft(draft.draft_id)
        return draft

    async def cancel_request(
        self,
        request_id: str,
        *,
        user_id: str | None,
        group_id: str | None,
        session_id: str | None,
    ) -> Any | None:
        request_id = str(request_id or "").strip()
        if not request_id:
            raise DraftWorkflowError("generation_request_not_found")
        draft, record = await self.resolve_request(request_id)
        if record is not None and not self._record_matches_owner(
            record,
            user_id=user_id,
            group_id=group_id,
            session_id=session_id,
        ):
            raise DraftWorkflowError("generation_request_forbidden")
        await self._set_cancel_record(
            request_id,
            user_id=user_id,
            group_id=group_id,
            session_id=session_id,
        )
        if draft is None:
            return None
        return await self.cancel_draft(draft.draft_id)

    async def _cancel_comfy_prompt(
        self,
        prompt_id: str,
        *,
        server_url: str | None = None,
    ) -> None:
        import httpx

        prompt_id = str(prompt_id or "")
        if not prompt_id:
            return
        base_url = (server_url or self._server_url()).rstrip("/")
        try:
            async with httpx.AsyncClient(
                timeout=self._comfyui_config.connect_timeout
            ) as client:
                queue_response = await client.get(f"{base_url}/queue")
                if queue_response.status_code != 200:
                    return
                queue = queue_response.json()
                running_ids = {
                    item[1]
                    for item in queue.get("queue_running", [])
                    if isinstance(item, list) and len(item) > 1
                }
                pending_ids = {
                    item[1]
                    for item in queue.get("queue_pending", [])
                    if isinstance(item, list) and len(item) > 1
                }
                if prompt_id in pending_ids:
                    await client.post(
                        f"{base_url}/queue",
                        json={"delete": [prompt_id]},
                    )
                if prompt_id in running_ids:
                    await client.post(f"{base_url}/interrupt", json={})
        except (httpx.TimeoutException, httpx.TransportError, ValueError) as exc:
            _impl.logger.warning(
                "ComfyUI prompt cancellation failed",
                extra={"prompt_id": prompt_id, "error_type": type(exc).__name__},
            )

    async def _wait_for_prompt_release_after_cancel(
        self,
        prompt_id: str,
        *,
        server_url: str,
        timeout: float = 30.0,
    ) -> bool:
        await self._cancel_comfy_prompt(prompt_id, server_url=server_url)
        return await super()._wait_for_prompt_release_after_cancel(
            prompt_id,
            server_url=server_url,
            timeout=timeout,
        )

    async def cancel_draft(self, draft_id: str) -> Any:
        draft = await self.get_draft(draft_id)
        if draft is None:
            raise DraftWorkflowError("generation_request_not_found")
        if draft.status in _TERMINAL_DRAFT_STATUSES:
            return draft

        request_id = str(draft.generation_params.get("request_id") or "")
        await self._set_cancel_record(
            request_id,
            user_id=draft.user_id,
            group_id=draft.group_id,
            session_id=draft.session_id,
            ttl_seconds=max(1, int(draft.expires_at - time.time())),
        )

        tasks: set[asyncio.Task[Any]] = set()
        task = self._draft_tasks.get(draft_id)
        if task is not None and not task.done():
            tasks.add(task)
        confirmation = self._confirmation_tasks.get(draft_id)
        if confirmation is not None and not confirmation.done():
            tasks.add(confirmation)
        active_names = {
            f"draft-generate-{draft_id}",
            f"draft-regenerate-{draft_id}",
            f"draft-confirm-{draft_id}",
        }
        tasks.update(
            candidate
            for candidate in self._bg_tasks
            if not candidate.done() and candidate.get_name() in active_names
        )
        for owned_task in tasks:
            if owned_task is not asyncio.current_task():
                owned_task.cancel()
        if tasks:
            await asyncio.gather(
                *(task for task in tasks if task is not asyncio.current_task()),
                return_exceptions=True,
            )

        if draft.comfy_prompt_id:
            worker = (
                self._gpu_coordinator.get_worker(draft.worker_id)
                if self._gpu_coordinator is not None and draft.worker_id
                else None
            )
            await self._cancel_comfy_prompt(
                draft.comfy_prompt_id,
                server_url=worker.server_url if worker is not None else None,
            )

        draft.status = DRAFT_STATUS_CANCELLED
        draft.stage = "cancelled"
        draft.progress = 0.0
        draft.error = "cancelled"
        draft.comfy_prompt_id = None
        draft.generation_params["cancelled_at"] = time.time()
        draft.generation_params["progress_source"] = "stage"
        ttl_remaining = max(1, int(draft.expires_at - time.time()))
        await super()._store_draft(draft, ttl_remaining)
        if self._task_tracker is not None:
            try:
                await self._task_tracker.update_status(
                    "draft",
                    draft_id,
                    "cancelled",
                    metadata={"request_id": request_id},
                )
            except Exception as exc:
                _impl.logger.debug("TaskTracker cancel update failed: %s", exc)
        await self._emit_draft_trace(
            str(draft.generation_params.get("trace_id") or ""),
            "draft.cancelled",
            payload={"draft_id": draft_id, "request_id": request_id},
        )
        return draft

    async def confirm_draft(self, draft_id: str):
        """Coalesce concurrent confirmation requests for the same draft."""
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
        if (
            draft.status != DRAFT_STATUS_CANCELLED
            and await self._draft_cancel_requested(draft.draft_id)
        ):
            draft.status = DRAFT_STATUS_CANCELLED
            draft.stage = "cancelled"
            draft.progress = 0.0
            draft.error = "cancelled"
            draft.comfy_prompt_id = None
            draft.generation_params["progress_source"] = "stage"
            await super()._store_draft(draft, ttl_seconds)
            return
        self._freeze_video_keyframe(draft)
        await super()._store_draft(draft, ttl_seconds)

    async def _regenerate_draft(self, old_draft):
        if old_draft.media_type == "video":
            params = old_draft.generation_params
            if params.get("source_image_sha256") and not params.get(
                "source_image_frozen_draft_id"
            ):
                params["source_image_frozen_draft_id"] = old_draft.draft_id
        return await super()._regenerate_draft(old_draft)

    async def _claim_draft_confirmation(self, draft_id: str):
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
                if await self._draft_cancel_requested(draft_id):
                    raise asyncio.CancelledError
                return await operation(), None
            if not self._pool_has_worker(status, capability):
                raise DraftWorkflowError(_GPU_TOPOLOGY_ERROR)

        worker_task = asyncio.create_task(
            super()._run_on_comfy_worker(
                draft_id,
                capability,
                operation,
                preferred_worker_id=preferred_worker_id,
                memory_requirement_gb=memory_requirement_gb,
            ),
            name=f"draft-worker-{draft_id}",
        )
        try:
            while not worker_task.done():
                if await self._draft_cancel_requested(draft_id):
                    worker_task.cancel()
                    await asyncio.gather(worker_task, return_exceptions=True)
                    raise asyncio.CancelledError
                await asyncio.sleep(0.25)
            return await worker_task
        except asyncio.CancelledError:
            if not worker_task.done():
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
            raise

    async def _mark_draft_confirmation_failed(
        self,
        draft: Any,
        reason: str,
    ) -> None:
        if await self._draft_cancel_requested(draft.draft_id):
            await self.cancel_draft(draft.draft_id)
            return
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
