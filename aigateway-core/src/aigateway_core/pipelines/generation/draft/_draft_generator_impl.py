"""
Draft Generator Strategy — 渐进式生成工作流核心逻辑
===================================================

管理 Draft-to-HiRes 工作流：
1. 生成低分辨率草图（图片默认 1024x1024 / 视频关键帧）
2. 确认后触发 Upscaler 放大到目标分辨率
3. 拒绝后重新生成（不缓存被拒绝的草图，立即释放资源）
4. 重试次数限制，耗尽后返回错误并保留最近草图
5. draft_id 唯一标识，24 小时过期自动释放
6. ComfyUI API 集成：ComfyUI 为必需生成后端，不静默回退外部媒体 API

需求: 3.1, 3.2, 3.3, 3.4, 3.5, 3.7, 3.8, 3.9, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import time
import uuid
from io import BytesIO
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

from PIL import Image

from aigateway_core.pipelines.generation._common.config import DraftWorkflowConfig
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation._common.models import (
    DRAFT_STATUS_COMPLETED,
    DRAFT_STATUS_CONFIRMED,
    DRAFT_STATUS_CONFIRMING,
    DRAFT_STATUS_FAILED,
    DRAFT_STATUS_GENERATING,
    DRAFT_STATUS_PENDING,
    DRAFT_STATUS_QUEUED,
    DRAFT_STATUS_REFINING,
    DRAFT_STATUS_RUNNING,
    DraftResult,
    GenerationRequest,
    UpscaleResult,
)
from aigateway_core.shared.integration_configs import ComfyUIConfig

logger = logging.getLogger(__name__)

# Redis key prefix for draft storage (元数据/状态; previews/result bytes 落盘文件)
_DRAFT_KEY_PREFIX = "aigateway:draft"
# Redis set: 记录一个 session 下所有 draft_id，供 delete_session 批量删 key
_DRAFT_SESSION_KEY_PREFIX = "aigateway:draft:session"
_MODELS_SIZE_CACHE_TTL_SECONDS = 60.0
_DRAFT_RUNTIME_STALE_GRACE_SECONDS = 60.0

# Default negative prompt for image generation
_DEFAULT_NEGATIVE_PROMPT = "ugly, blurry, low quality, distorted, deformed"
_SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class DraftGeneratorStrategy:
    """草图生成器 — 管理 Draft-to-HiRes 工作流.

    负责生成低分辨率草图供用户预览确认，确认后执行高清放大，
    拒绝后重新生成。

    存储双层设计：
    - Redis (`aigateway:draft:{draft_id}`) 存轻量元数据 + status，
      供前端轮询快速读状态（不读 bytes）。
    - 文件 (`{store_dir}/{session_id}/{draft_id}/`) 存 previews/result bytes
      + meta.json。随会话生命周期由 DraftSessionCleaner 清理，不受 Redis TTL 影响。
    - 生成异步化：submit_draft 立即返回 draft_id (status=generating)，
      后台 _generate_draft_async 跑 ComfyUI，完成写 preview.bin + status=pending。

    图片草稿与确认必须使用 ComfyUI；服务不可用或执行失败时明确失败。
    视频最终生成由 ``ComfyUIConfig.video_enabled`` 显式控制。

    Attributes:
        _config: Draft 工作流配置
        _redis_client: Redis 客户端实例（需支持 async get/set/delete/expire/sadd/srem/smembers）
        _store_dir: 草稿文件存储根目录
        _task_tracker: TaskTracker 实例（延迟绑定，追踪异步生成任务状态）
        _comfyui_config: ComfyUI API 连接配置
        _comfyui_available: ComfyUI 服务是否可用
    """

    def __init__(
        self,
        config: DraftWorkflowConfig,
        redis_client: Any = None,
        comfyui_config: ComfyUIConfig | None = None,
        store_dir: str | None = None,
        task_tracker: Any = None,
    ) -> None:
        """初始化 DraftGeneratorStrategy.

        Args:
            config: Draft-to-HiRes 工作流配置
            redis_client: Redis 客户端实例。若为 None，则使用内存字典模拟。
            comfyui_config: ComfyUI API 连接配置。若为 None，使用默认配置。
            store_dir: 草稿文件存储根目录。None 时取 config.store_dir。
            task_tracker: TaskTracker 实例。可延迟绑定（由 main.py 注入）。
        """
        self._config = config
        self._redis_client = redis_client
        self._store_dir = store_dir or getattr(config, "store_dir", "/app/data/drafts")
        self._task_tracker = task_tracker
        self._comfyui_config = comfyui_config or ComfyUIConfig()
        self._comfyui_available: bool = False
        self._comfyui_semaphore = asyncio.Semaphore(
            max(1, int(self._comfyui_config.max_concurrency))
        )
        # 仅保留给显式人工降级路径；正常草稿/确认链路不调用 provider bridge。
        self._litellm_bridge: Any = None
        self._draft_state_lock = asyncio.Lock()
        self._models_size_cache: tuple[float, int] | None = None
        self._models_size_cache_lock = asyncio.Lock()
        # In-memory fallback when no Redis client is provided (for testing)
        self._memory_store: dict[str, str] = {}
        # session → set(draft_id) 的内存镜像（无 Redis 时测试用）
        self._memory_session_index: dict[str, set] = {}
        # 后台生成任务强引用集合。asyncio.create_task 返回的 Task 仅被事件循环的
        # WeakSet 持有，CPython GC 可能在 submit_draft 返回后回收未完成的协程，
        # 导致 Redis 状态永久卡在 generating、前端轮询到超时。这里持有强引用，
        # 任务完成后通过 add_done_callback 自动移除。
        self._bg_tasks: set = set()

    @property
    def checkpoint_name(self) -> str:
        return self._comfyui_config.checkpoint_name

    async def shutdown(self) -> None:
        """Cancel and await every owned background generation task."""
        tasks = tuple(task for task in self._bg_tasks if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._bg_tasks.difference_update(tasks)

    async def generate_draft(
        self,
        request: GenerationRequest,
        config: DraftWorkflowConfig,
        keyframe_count: int | None = None,
        chat_session_id: str | None = None,
        user_id: str | None = None,
        group_id: str | None = None,
    ) -> DraftResult:
        """提交草稿生成任务（异步）— 立即返回 draft_id，后台生成预览.

        生成被拆为两阶段（方案 B 异步化）：
        1. submit_draft：生成 draft_id、写 meta（status=generating）、注册 TaskTracker、
           asyncio.create_task 起后台生成、立即返回 DraftResult（previews 空，status=generating）。
           不阻塞 dispatcher —— 前端拿到 draft_id 后轮询 GET /admin/draft/{id}/preview。
        2. _generate_draft_async（后台）：跑 ComfyUI 生成，完成写 preview.bin + status=pending。

        图片请求: 生成低分辨率预览（单张，默认 1024x1024）
        视频请求: 按时间间隔动态生成关键帧

        Args:
            request: 生成请求
            config: Draft 工作流配置（允许运行时覆盖）
            keyframe_count: 用户显式指定的关键帧数量，覆盖间隔计算
            chat_session_id: 聊天会话 ID（文件存储/会话级清理用）
            user_id: 草稿所有者
            group_id: 草稿所属群组

        Returns:
            DraftResult（status=generating，previews 为空）。前端据 draft_id 轮询取预览。
        """
        return await self.submit_draft(
            request, config, keyframe_count, chat_session_id, user_id, group_id
        )

    async def submit_draft(
        self,
        request: GenerationRequest,
        config: DraftWorkflowConfig,
        keyframe_count: int | None = None,
        chat_session_id: str | None = None,
        user_id: str | None = None,
        group_id: str | None = None,
    ) -> DraftResult:
        """提交草稿生成任务（异步）— 立即返回 draft_id (status=generating)."""
        draft_id = uuid.uuid4().hex
        now = time.time()
        ttl_seconds = config.retention_period_hours * 3600
        expires_at = now + ttl_seconds

        is_video = self._is_video_request(request)
        media_type = "video" if is_video else "image"
        seed = int(uuid.uuid4().int % (2**32))
        uses_qwen_image = self._should_use_qwen_image(request)

        # generation_params 快照（后台 task 也要用，这里先建好）
        generation_params: dict[str, Any] = {
            "prompt": request.prompt,
            "target_resolution": list(request.target_resolution),
            "media_type": media_type,
            "draft_resolution": list(config.draft_resolution),
            "request_id": request.request_id,
            "seed": seed,
            "checkpoint": (
                self._comfyui_config.qwen_image_diffusion_model
                if uses_qwen_image
                else self._comfyui_config.checkpoint_name
            ),
            "preset_id": (
                request.preset_id
                or ("qwen-image" if uses_qwen_image else "sdxl-draft")
            ),
            "workflow_version": self._comfyui_config.workflow_version,
            "quality": request.quality,
            "trace_id": request.trace_id,
        }
        if is_video and keyframe_count is not None:
            generation_params["explicit_keyframe_count"] = keyframe_count
        if is_video:
            generation_params["video_workflow_version"] = (
                self._comfyui_config.video_workflow_version
            )

        # 占位 DraftResult：status=generating，previews 空
        draft = DraftResult(
            draft_id=draft_id,
            previews=[],
            generation_params=generation_params,
            created_at=now,
            expires_at=expires_at,
            attempt_number=1,
            max_attempts=config.max_regeneration_attempts,
            status=DRAFT_STATUS_QUEUED,
            media_type=media_type,
            session_id=chat_session_id,
            user_id=user_id,
            group_id=group_id,
            progress=0.0,
            stage="queued",
            workflow_version=self._comfyui_config.workflow_version,
        )

        # 写 meta + Redis 元数据（status=generating）—— 前端轮询据此知道在生成中
        await self._store_draft(draft, ttl_seconds)

        # 注册 TaskTracker（供 /admin/chat/tasks 列出未完成任务）
        if self._task_tracker is not None:
            try:
                await self._task_tracker.register(
                    task_type="draft",
                    task_id=draft_id,
                    metadata={
                        "session_id": chat_session_id,
                        "user_id": user_id,
                        "group_id": group_id,
                        "media_type": media_type,
                        "request_id": request.request_id,
                        "trace_id": request.trace_id,
                    },
                    ttl_seconds=ttl_seconds,
                )
            except Exception as exc:
                logger.warning("TaskTracker register draft failed: %s", exc)

        await self._emit_draft_trace(
            request.trace_id,
            "draft.queued",
            payload={
                "draft_id": draft_id,
                "media_type": media_type,
                "request_id": request.request_id,
            },
        )

        # 起后台生成任务（不 await —— 立即返回 draft_id）。
        # 必须持有强引用：事件循环仅用 WeakSet 跟踪 Task，若被 GC 回收则协程
        # 永不执行、Redis 状态卡 generating（见 _bg_tasks 注释）。
        bg_task = asyncio.create_task(
            self._generate_draft_async(
                draft_id=draft_id,
                request=request,
                config=config,
                keyframe_count=keyframe_count,
                is_video=is_video,
                media_type=media_type,
                generation_params=generation_params,
                ttl_seconds=ttl_seconds,
                expires_at=expires_at,
                chat_session_id=chat_session_id,
                user_id=user_id,
                group_id=group_id,
            ),
            name=f"draft-generate-{draft_id}",
        )
        self._bg_tasks.add(bg_task)
        bg_task.add_done_callback(self._bg_tasks.discard)

        logger.info(
            "generation_optimization.draft_generator.draft_submitted",
            extra={
                "draft_id": draft_id,
                "media_type": media_type,
                "expires_at": expires_at,
                "request_id": request.request_id,
            },
        )

        return draft

    async def _emit_draft_trace(
        self,
        trace_id: str | None,
        name: str,
        *,
        status: str = "ok",
        duration_ms: float | None = None,
        payload: dict[str, Any] | None = None,
        stage: str = "draft",
    ) -> None:
        if not trace_id:
            return
        try:
            from aigateway_core.shared.trace_event import append_trace_event

            await append_trace_event(
                self._redis_client,
                trace_id=trace_id,
                stage=stage,
                name=name,
                duration_ms=duration_ms,
                status=status,
                payload=payload,
            )
        except Exception as exc:
            logger.warning(
                "draft trace append failed",
                extra={
                    "trace_id": trace_id,
                    "event_name": name,
                    "error_type": type(exc).__name__,
                },
            )

    async def _generate_draft_async(
        self,
        draft_id: str,
        request: GenerationRequest,
        config: DraftWorkflowConfig,
        keyframe_count: int | None,
        is_video: bool,
        media_type: str,
        generation_params: dict[str, Any],
        ttl_seconds: int,
        expires_at: float,
        chat_session_id: str | None,
        user_id: str | None,
        group_id: str | None,
    ) -> None:
        """后台生成预览（由 submit_draft 用 asyncio.create_task 起动）.

        完成后：写 preview.bin + 更新 meta/Redis status=pending + TaskTracker succeeded。
        失败：status=failed + TaskTracker failed，错误写 meta。
        """
        start_time = time.monotonic()
        attempt_number = 1
        max_attempts = config.max_regeneration_attempts
        trace_id = request.trace_id or str(generation_params.get("trace_id") or "")
        try:
            running = await self._load_draft(draft_id)
            if running is not None:
                attempt_number = running.attempt_number
                max_attempts = running.max_attempts
                running.status = DRAFT_STATUS_RUNNING
                running.stage = "running"
                running.progress = 0.1
                running.generation_params["progress_source"] = "stage"
                await self._store_draft(running, ttl_seconds)
                await self._emit_draft_trace(
                    trace_id,
                    "draft.running",
                    payload={"draft_id": draft_id, "media_type": media_type},
                )

            if is_video:
                previews = await self._generate_video_previews_with_comfyui(
                    request,
                    config,
                    seed=int(generation_params["seed"]),
                    draft_id=draft_id,
                )
            else:
                previews = [
                    await self._generate_image_preview_with_comfyui(
                        request,
                        config,
                        seed=int(generation_params["seed"]),
                        draft_id=draft_id,
                    )
                ]

            gpu_seconds = max(0.0, time.monotonic() - start_time)
            draft = DraftResult(
                draft_id=draft_id,
                previews=previews,
                generation_params=generation_params,
                created_at=time.time(),
                expires_at=expires_at,
                attempt_number=attempt_number,
                max_attempts=max_attempts,
                status=DRAFT_STATUS_PENDING,
                media_type=media_type,
                session_id=chat_session_id,
                user_id=user_id,
                group_id=group_id,
                progress=1.0,
                stage="pending",
                workflow_version=self._comfyui_config.workflow_version,
                gpu_seconds=gpu_seconds,
            )
            draft.generation_params["progress_source"] = "complete"
            await self._store_draft(draft, max(1, int(expires_at - time.time())))
            from aigateway_core.pipelines.generation._common.metrics import (
                get_prometheus_registry,
            )
            get_prometheus_registry().inc_comfyui_gpu_seconds(
                "draft", media_type, gpu_seconds
            )

            if self._task_tracker is not None:
                try:
                    await self._task_tracker.update_status(
                        "draft", draft_id, "succeeded",
                        metadata={"preview_count": len(previews)},
                    )
                except Exception as exc:
                    logger.debug("TaskTracker update succeeded failed: %s", exc)

            await self._emit_draft_trace(
                trace_id,
                "draft.preview_ready",
                duration_ms=round(gpu_seconds * 1000, 2),
                payload={
                    "draft_id": draft_id,
                    "media_type": media_type,
                    "preview_count": len(previews),
                    "gpu_seconds": round(gpu_seconds, 2),
                },
            )

            logger.info(
                "generation_optimization.draft_generator.draft_created",
                extra={
                    "draft_id": draft_id,
                    "media_type": media_type,
                    "preview_count": len(previews),
                    "expires_at": expires_at,
                    "request_id": request.request_id,
                    "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
                },
            )
        except Exception as exc:
            logger.error(
                "generation_optimization.draft_generator.async_failed",
                extra={"draft_id": draft_id, "error": str(exc)},
                exc_info=True,
            )
            await self._emit_draft_trace(
                trace_id,
                "draft.preview_failed",
                status="error",
                duration_ms=round((time.monotonic() - start_time) * 1000, 2),
                payload={
                    "draft_id": draft_id,
                    "media_type": media_type,
                    "error": type(exc).__name__,
                },
            )
            # 标记 failed（写 meta + Redis，前端轮询据此报错）
            try:
                draft_dir = self._ensure_draft_dir(chat_session_id, draft_id)
                meta = self._read_meta(draft_dir) or {}
                error_text = str(exc).lower()
                if "gpu_out_of_memory" in error_text or "out of memory" in error_text:
                    public_error = "comfyui_gpu_out_of_memory"
                elif "执行超时" in error_text or "timeout" in error_text:
                    public_error = "comfyui_execution_timeout"
                elif "storage" in error_text:
                    public_error = "comfyui_storage_low"
                else:
                    public_error = "comfyui_generation_failed"
                meta.update({
                    "draft_id": draft_id,
                    "session_id": chat_session_id,
                    "user_id": user_id,
                    "group_id": group_id,
                    "media_type": media_type,
                    "status": DRAFT_STATUS_FAILED,
                    "stage": "failed",
                    "progress": 0.0,
                    "expires_at": expires_at,
                    "error": public_error,
                })
                self._write_meta_dict(draft_dir, meta)

                # 更新 Redis 元数据 status=failed
                key = self._make_redis_key(draft_id)
                if self._redis_client is not None:
                    raw = await self._redis_client.get(key)
                else:
                    raw = self._memory_store.get(key)
                if raw is not None:
                    raw = raw.decode() if isinstance(raw, bytes) else raw
                    data = json.loads(raw)
                    data["status"] = DRAFT_STATUS_FAILED
                    data["stage"] = "failed"
                    data["progress"] = 0.0
                    data["error"] = public_error
                    ttl_remaining = max(1, int(expires_at - time.time()))
                    if self._redis_client is not None:
                        await self._redis_client.set(key, json.dumps(data), ex=ttl_remaining)
                    else:
                        self._memory_store[key] = json.dumps(data)
            except Exception:
                logger.error("failed to mark draft %s as failed", draft_id, exc_info=True)

            if self._task_tracker is not None:
                try:
                    await self._task_tracker.update_status(
                        "draft", draft_id, "failed", metadata={"error": str(exc)}
                    )
                except Exception as exc2:
                    logger.debug("TaskTracker update failed failed: %s", exc2)


    async def confirm_draft(self, draft_id: str) -> UpscaleResult:
        """确认草图并执行高清放大.

        验证草图状态为 pending，然后触发 Upscaler 放大到目标分辨率。

        Args:
            draft_id: 草图唯一标识

        Returns:
            UpscaleResult 包含放大后的数据和算法信息

        Raises:
            DraftWorkflowError: 草图不存在、已过期或状态非 pending
        """
        draft, claimed = await self._claim_draft_confirmation(draft_id)
        if draft is None:
            raise DraftWorkflowError(
                f"Draft not found or expired: {draft_id}"
            )

        # Check if draft has expired
        if time.time() > draft.expires_at:
            raise DraftWorkflowError(
                f"Draft has expired: {draft_id}"
            )

        if draft.status in (DRAFT_STATUS_CONFIRMED, DRAFT_STATUS_COMPLETED):
            draft_dir = self._draft_dir(draft.session_id, draft_id)
            output_data = self._read_result_bytes(draft_dir)
            if output_data is not None:
                resolution = (
                    (
                        self._comfyui_config.video_width,
                        self._comfyui_config.video_height,
                    )
                    if draft.media_type == "video"
                    else self._reported_image_resolution(
                        output_data, self._get_target_resolution(draft)
                    )
                )
                return UpscaleResult(
                    draft_id=draft_id,
                    output_data=output_data,
                    target_resolution=resolution,
                    algorithm_used=draft.generation_params.get(
                        "confirmed_algorithm", self._config.upscale_algorithm
                    ),
                    duration_ms=0.0,
                )
            raise DraftWorkflowError(
                f"Draft cannot be confirmed: status is '{draft.status}', "
                f"but no persisted result exists. draft_id={draft_id}"
            )

        if not claimed or draft.status not in (
            DRAFT_STATUS_CONFIRMING,
            DRAFT_STATUS_REFINING,
        ):
            raise DraftWorkflowError(
                f"Draft cannot be confirmed: status is '{draft.status}', "
                f"expected 'pending'. draft_id={draft_id}"
            )

        is_video = draft.media_type == "video"
        target_resolution = (
            (self._comfyui_config.video_width, self._comfyui_config.video_height)
            if is_video
            else self._get_target_resolution(draft)
        )
        start_time = time.monotonic()

        try:
            await self._check_comfyui()
            if is_video:
                output_data = await self._generate_video_with_comfyui(draft)
                algorithm_used = (
                    f"comfyui:{self._comfyui_config.video_workflow_version}"
                )
            else:
                output_data = await self._upscale_with_comfyui(
                    draft, target_resolution
                )
                quality = str(draft.generation_params.get("quality", "standard"))
                algorithm_used = (
                    f"comfyui:realesrgan:{self._comfyui_config.upscale_model}"
                    if quality == "faithful_4k"
                    else f"comfyui:{self._comfyui_config.workflow_version}"
                )
            if output_data is None:
                raise DraftWorkflowError("ComfyUI returned no confirmed media")
            actual_resolution = (
                target_resolution
                if is_video
                else self._reported_image_resolution(output_data, target_resolution)
            )
        except Exception:
            await self._mark_draft_confirmation_failed(
                draft, f"{draft.media_type} confirmation failed"
            )
            raise

        duration_ms = (time.monotonic() - start_time) * 1000.0

        result = UpscaleResult(
            draft_id=draft_id,
            output_data=output_data,
            target_resolution=actual_resolution,
            algorithm_used=algorithm_used,
            duration_ms=duration_ms,
        )
        from aigateway_core.pipelines.generation._common.metrics import (
            get_prometheus_registry,
        )
        get_prometheus_registry().inc_comfyui_gpu_seconds(
            "refine", draft.media_type, duration_ms / 1000.0
        )

        # 持久化高清结果到文件（修复原"confirm 后 output_data 仅内存返回、未落盘"bug）。
        # GET /admin/draft/{id}/result 通过 get_result_bytes 读取此文件，刷新后可重取。
        try:
            draft_dir = self._ensure_draft_dir(draft.session_id, draft_id)
            self._write_result_bytes(draft_dir, output_data)
            draft.status = DRAFT_STATUS_COMPLETED
            draft.stage = "completed"
            draft.progress = 1.0
            draft.gpu_seconds += max(0.0, duration_ms / 1000.0)
            draft.generation_params["confirmed_algorithm"] = algorithm_used
            draft.generation_params["progress_source"] = "complete"
            ttl_remaining = max(1, int(draft.expires_at - time.time()))
            await self._store_draft(draft, ttl_remaining)
        except Exception as exc:
            logger.warning("draft result persist failed (draft_id=%s): %s", draft_id, exc)

        logger.info(
            "generation_optimization.draft_generator.draft_confirmed",
            extra={
                "draft_id": draft_id,
                "target_resolution": actual_resolution,
                "algorithm": algorithm_used,
                "duration_ms": duration_ms,
            },
        )
        return result

    async def _claim_draft_confirmation(self, draft_id: str) -> tuple[DraftResult | None, bool]:
        """Atomically move a pending draft to confirming.

        This is the idempotency gate for confirm actions. Only the first caller
        is allowed to leave ``pending`` and invoke the expensive upstream work.
        Later retries either reuse a persisted confirmed result or get a state
        conflict without submitting another video/upscale task.
        """
        key = self._make_redis_key(draft_id)

        if self._redis_client is None:
            async with self._draft_state_lock:
                draft = await self._load_draft(draft_id)
                if draft is None:
                    return None, False
                if draft.status == DRAFT_STATUS_PENDING and time.time() <= draft.expires_at:
                    draft.status = DRAFT_STATUS_REFINING
                    draft.stage = "refining"
                    draft.progress = 0.25
                    ttl_remaining = max(1, int(draft.expires_at - time.time()))
                    await self._store_draft(draft, ttl_remaining)
                    return draft, True
                return draft, False

        script = """
local raw = redis.call('GET', KEYS[1])
if not raw then return {0, ''} end
local data = cjson.decode(raw)
if tonumber(ARGV[1]) > tonumber(data['expires_at'] or '0') then
  return {2, raw}
end
if data['status'] == ARGV[2] then
  data['status'] = ARGV[3]
  redis.call('SET', KEYS[1], cjson.encode(data), 'EX', tonumber(ARGV[4]))
  return {1, cjson.encode(data)}
end
return {3, raw}
"""
        now = time.time()
        existing = await self._load_draft(draft_id)
        ttl_remaining = max(1, int(existing.expires_at - now)) if existing else 1
        redis_conn = getattr(self._redis_client, "redis", self._redis_client)
        try:
            result = await redis_conn.eval(
                script,
                1,
                key,
                now,
                DRAFT_STATUS_PENDING,
                DRAFT_STATUS_REFINING,
                ttl_remaining,
            )
        except (AttributeError, TypeError):
            async with self._draft_state_lock:
                draft = await self._load_draft(draft_id)
                if draft is None:
                    return None, False
                if draft.status == DRAFT_STATUS_PENDING and now <= draft.expires_at:
                    draft.status = DRAFT_STATUS_REFINING
                    draft.stage = "refining"
                    draft.progress = 0.25
                    ttl_remaining = max(1, int(draft.expires_at - now))
                    await self._store_draft(draft, ttl_remaining)
                    return draft, True
                return draft, False

        code = int(result[0])
        raw = result[1]
        if code == 0:
            return None, False
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if raw:
            data = json.loads(raw)
            draft = self._draft_from_serialized(draft_id, data)
            if code == 1:
                draft.stage = "refining"
                draft.progress = 0.25
                await self._store_draft(draft, ttl_remaining)
            return draft, code == 1
        return await self._load_draft(draft_id), False

    async def _mark_draft_confirmation_failed(self, draft: DraftResult, reason: str) -> None:
        draft.status = DRAFT_STATUS_PENDING
        draft.stage = "pending"
        draft.progress = 1.0
        draft.generation_params["last_confirm_error"] = reason
        ttl_remaining = max(1, int(draft.expires_at - time.time()))
        await self._store_draft(draft, ttl_remaining)

    async def reject_draft(self, draft_id: str) -> DraftResult:
        """拒绝草图并重新生成.

        验证草图状态为 pending，检查重试次数未达上限，
        然后删除被拒绝的草图（不缓存、立即释放），生成新草图。

        Args:
            draft_id: 被拒绝的草图标识

        Returns:
            新生成的 DraftResult

        Raises:
            DraftWorkflowError: 草图不存在、状态非 pending 或重试次数耗尽
        """
        draft = await self._load_draft(draft_id)
        if draft is None:
            raise DraftWorkflowError(
                f"Draft not found or expired: {draft_id}"
            )

        if draft.status != DRAFT_STATUS_PENDING:
            raise DraftWorkflowError(
                f"Draft cannot be rejected: status is '{draft.status}', "
                f"expected 'pending'. draft_id={draft_id}"
            )

        # Check regeneration limit
        if draft.attempt_number >= draft.max_attempts:
            raise DraftWorkflowError(
                f"Regeneration limit reached: {draft.attempt_number}/{draft.max_attempts} "
                f"attempts used. draft_id={draft_id}"
            )

        # Delete the rejected draft immediately (don't cache, release resources)
        await self._delete_draft(draft_id)

        logger.info(
            "generation_optimization.draft_generator.draft_rejected",
            extra={
                "draft_id": draft_id,
                "attempt_number": draft.attempt_number,
                "max_attempts": draft.max_attempts,
            },
        )
        from aigateway_core.pipelines.generation._common.metrics import (
            get_prometheus_registry,
        )
        get_prometheus_registry().inc_draft_discarded(draft.media_type)

        # Generate new draft with incremented attempt number
        new_draft = await self._regenerate_draft(draft)

        return new_draft

    async def get_draft(self, draft_id: str) -> DraftResult | None:
        """获取草图信息.

        Args:
            draft_id: 草图唯一标识

        Returns:
            DraftResult 或 None（不存在/已过期）
        """
        return await self._load_draft(draft_id)

    async def sync_draft_runtime_state(self, draft_id: str) -> DraftResult | None:
        """Reconcile an in-progress draft with the real ComfyUI runtime state.

        Browser polling can outlive the Python background task that originally
        submitted the ComfyUI job (for example after a gateway restart). Without
        this reconciliation, the persisted draft may stay at 10%/50% forever
        even though ComfyUI no longer has matching work. This method is called
        from read-only status/preview endpoints to fail stale drafts closed or
        recover a completed preview from ComfyUI history.
        """
        draft = await self._load_draft(draft_id)
        if draft is None:
            return None
        in_progress = {
            DRAFT_STATUS_GENERATING,
            DRAFT_STATUS_QUEUED,
            DRAFT_STATUS_RUNNING,
            DRAFT_STATUS_REFINING,
        }
        if draft.status not in in_progress:
            return draft

        age_seconds = max(0.0, time.time() - float(draft.created_at or 0.0))
        prompt_id = str(draft.comfy_prompt_id or "")
        trace_id = str(draft.generation_params.get("trace_id") or "")

        if not prompt_id:
            if age_seconds < _DRAFT_RUNTIME_STALE_GRACE_SECONDS:
                return draft
            return await self._mark_in_progress_draft_lost(
                draft,
                "draft_worker_lost",
                "Draft background worker disappeared before submitting ComfyUI job",
            )

        try:
            prompt_state = await self._get_comfy_prompt_state(prompt_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "generation_optimization.draft_generator.runtime_sync_transient_error",
                extra={
                    "draft_id": draft_id,
                    "prompt_id": prompt_id,
                    "error_type": type(exc).__name__,
                },
            )
            return draft
        if prompt_state == "unknown":
            return draft
        if prompt_state in {"queued", "running"}:
            return draft
        if prompt_state == "completed":
            if draft.status == DRAFT_STATUS_REFINING:
                return draft
            try:
                previews = await self._poll_results(
                    prompt_id,
                    timeout=1,
                    trace_id=trace_id,
                    draft_id=draft_id,
                )
            except Exception as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return await self._mark_in_progress_draft_lost(
                    draft,
                    "comfyui_recovery_failed",
                    f"Completed ComfyUI job could not be recovered: {type(exc).__name__}",
                )
            draft.previews = previews
            draft.status = DRAFT_STATUS_PENDING
            draft.stage = "pending"
            draft.progress = 1.0
            await self._store_draft(draft, max(1, int(draft.expires_at - time.time())))
            if self._task_tracker is not None:
                try:
                    await self._task_tracker.update_status(
                        "draft", draft_id, "succeeded",
                        metadata={"preview_count": len(previews), "recovered": True},
                    )
                except Exception as exc:
                    logger.debug("TaskTracker recovery update failed: %s", exc)
            await self._emit_draft_trace(
                trace_id,
                "draft.preview_recovered",
                payload={"draft_id": draft_id, "prompt_id": prompt_id},
            )
            return draft

        if age_seconds < _DRAFT_RUNTIME_STALE_GRACE_SECONDS:
            return draft
        if draft.status == DRAFT_STATUS_REFINING:
            await self._mark_draft_confirmation_failed(
                draft,
                "ComfyUI refinement job disappeared",
            )
            return await self._load_draft(draft_id)
        return await self._mark_in_progress_draft_lost(
            draft,
            "comfyui_job_lost",
            "ComfyUI job is no longer queued, running, or present in history",
        )

    async def _get_comfy_prompt_state(self, prompt_id: str) -> str:
        """Return queued/running/completed/missing/unknown for a ComfyUI prompt."""
        import httpx

        base_url = self._comfyui_config.server_url
        try:
            async with httpx.AsyncClient(
                timeout=self._comfyui_config.connect_timeout
            ) as client:
                queue_response = await client.get(f"{base_url}/queue")
                if queue_response.status_code == 200:
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
                    if prompt_id in running_ids:
                        return "running"
                    if prompt_id in pending_ids:
                        return "queued"

                history_response = await client.get(f"{base_url}/history/{prompt_id}")
                if history_response.status_code == 200:
                    history = history_response.json()
                    if prompt_id in history:
                        return "completed"
        except (httpx.TimeoutException, httpx.TransportError, ValueError) as exc:
            logger.debug(
                "ComfyUI prompt state check unavailable: prompt_id=%s error=%s",
                prompt_id,
                type(exc).__name__,
            )
            return "unknown"
        return "missing"

    async def _mark_in_progress_draft_lost(
        self,
        draft: DraftResult,
        code: str,
        reason: str,
    ) -> DraftResult:
        draft.status = DRAFT_STATUS_FAILED
        draft.stage = code
        draft.progress = 0.0
        draft.error = code
        draft.generation_params["last_runtime_error"] = reason
        await self._store_draft(draft, max(1, int(draft.expires_at - time.time())))
        if self._task_tracker is not None:
            try:
                await self._task_tracker.update_status(
                    "draft",
                    draft.draft_id,
                    "failed",
                    metadata={"error": code, "reason": reason},
                )
            except Exception as exc:
                logger.debug("TaskTracker lost update failed: %s", exc)
        await self._emit_draft_trace(
            str(draft.generation_params.get("trace_id") or ""),
            "draft.runtime_lost",
            status="error",
            payload={"draft_id": draft.draft_id, "error": code, "reason": reason},
        )
        return draft

    # ===================================================================
    # ComfyUI API 集成方法
    # ===================================================================

    async def _check_comfyui(self) -> None:
        """检测 ComfyUI 服务是否可达.

        通过 GET /system_stats 端点检测连接。
        设置 self._comfyui_available 标志。
        """
        import httpx

        url = f"{self._comfyui_config.server_url}/system_stats"
        try:
            async with httpx.AsyncClient(
                timeout=self._comfyui_config.connect_timeout
            ) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    self._comfyui_available = True
                    logger.info(
                        "generation_optimization.draft_generator.comfyui_connected",
                        extra={"server_url": self._comfyui_config.server_url},
                    )
                else:
                    self._comfyui_available = False
                    raise DraftWorkflowError(
                        f"ComfyUI health check failed: status={response.status_code}"
                    )
        except Exception as exc:
            self._comfyui_available = False
            if isinstance(exc, DraftWorkflowError):
                raise
            raise DraftWorkflowError("ComfyUI service is unavailable") from exc

    async def check_local_dependencies(self, request: GenerationRequest) -> None:
        """Fail before draft creation when an explicitly selected local path is unusable."""
        await self._check_comfyui()
        if self._should_use_qwen_image(request):
            diffusion, encoder, vae = self._validate_qwen_image_models()
            required: list[tuple[str, str]] = [
                ("diffusion_models", diffusion),
                ("text_encoders", encoder),
                ("vae", vae),
            ]
        else:
            required = [("checkpoints", self._validate_checkpoint())]
        if request.media_type == "video":
            diffusion, encoder, vae = self._validate_video_models()
            required.extend(
                [
                    ("diffusion_models", diffusion),
                    ("text_encoders", encoder),
                    ("vae", vae),
                ]
            )
        if request.quality == "faithful_4k" and request.media_type != "video":
            required.append(("upscale_models", self._validate_upscale_model()))
        root = self._comfyui_config.models_path
        missing = await asyncio.to_thread(
            lambda: [
                f"{folder}/{name}"
                for folder, name in required
                if not os.path.isfile(os.path.join(root, folder, name))
            ]
        )
        if missing:
            raise DraftWorkflowError(
                f"comfyui_missing_dependencies: {', '.join(missing)}"
            )

    @staticmethod
    def _directory_size(path: str) -> int:
        total = 0
        if not os.path.isdir(path):
            return 0
        for root, _dirs, files in os.walk(path):
            for filename in files:
                try:
                    total += os.path.getsize(os.path.join(root, filename))
                except OSError:
                    continue
        return total

    async def _get_models_size(self) -> int:
        """Return the models directory size without rescanning it per request."""
        now = time.monotonic()
        cached = self._models_size_cache
        if cached is not None and now - cached[0] < _MODELS_SIZE_CACHE_TTL_SECONDS:
            return cached[1]

        async with self._models_size_cache_lock:
            now = time.monotonic()
            cached = self._models_size_cache
            if cached is not None and now - cached[0] < _MODELS_SIZE_CACHE_TTL_SECONDS:
                return cached[1]
            size = await asyncio.to_thread(
                self._directory_size, self._comfyui_config.models_path
            )
            self._models_size_cache = (time.monotonic(), size)
            return size

    def _cleanup_expired_outputs(self) -> int:
        output_path = os.path.realpath(self._comfyui_config.output_path)
        if not os.path.isdir(output_path) or output_path in {"/", "/opt", "/app"}:
            return 0
        cutoff = time.time() - (
            self._comfyui_config.output_retention_hours * 3600
        )
        deleted = 0
        for root, _dirs, files in os.walk(output_path):
            real_root = os.path.realpath(root)
            if os.path.commonpath([output_path, real_root]) != output_path:
                continue
            for filename in files:
                path = os.path.join(real_root, filename)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.unlink(path)
                        deleted += 1
                except OSError:
                    continue
        return deleted

    async def _ensure_storage_capacity(self) -> None:
        """Fail closed before queuing expensive work when storage budgets are exceeded."""
        output_path = self._comfyui_config.output_path
        probe_path = output_path if os.path.exists(output_path) else self._store_dir
        while not os.path.exists(probe_path):
            parent = os.path.dirname(probe_path)
            if parent == probe_path:
                probe_path = "/"
                break
            probe_path = parent
        await asyncio.to_thread(self._cleanup_expired_outputs)
        usage = await asyncio.to_thread(shutil.disk_usage, probe_path)
        free_gb = usage.free / (1024**3)
        if free_gb < self._comfyui_config.min_free_gb:
            raise DraftWorkflowError(
                f"comfyui_storage_low: free={free_gb:.1f}GB, "
                f"required={self._comfyui_config.min_free_gb:.1f}GB"
            )

        models_size, output_size = await asyncio.gather(
            self._get_models_size(),
            asyncio.to_thread(
                self._directory_size, self._comfyui_config.output_path
            ),
        )
        models_gb = models_size / (1024**3)
        output_gb = output_size / (1024**3)
        if models_gb > self._comfyui_config.model_budget_gb:
            raise DraftWorkflowError(
                f"comfyui_model_budget_exceeded: used={models_gb:.1f}GB, "
                f"budget={self._comfyui_config.model_budget_gb:.1f}GB"
            )
        if output_gb > self._comfyui_config.output_budget_gb:
            raise DraftWorkflowError(
                f"comfyui_output_budget_exceeded: used={output_gb:.1f}GB, "
                f"budget={self._comfyui_config.output_budget_gb:.1f}GB"
            )

    def _validate_checkpoint(self) -> str:
        checkpoint = self._comfyui_config.checkpoint_name
        allowed = set(self._comfyui_config.allowed_checkpoints)
        if checkpoint not in allowed:
            raise DraftWorkflowError(
                f"ComfyUI checkpoint is not allowlisted: {checkpoint}"
            )
        return checkpoint

    def _validate_qwen_image_models(self) -> tuple[str, str, str]:
        config = self._comfyui_config
        approved = (
            (
                config.qwen_image_diffusion_model,
                set(config.allowed_qwen_image_diffusion_models),
                "diffusion model",
            ),
            (
                config.qwen_image_text_encoder,
                set(config.allowed_qwen_image_text_encoders),
                "text encoder",
            ),
            (
                config.qwen_image_vae,
                set(config.allowed_qwen_image_vaes),
                "VAE",
            ),
        )
        for name, allowlist, label in approved:
            if (
                name not in allowlist
                or not _SAFE_PATH_COMPONENT.fullmatch(name)
                or "/" in name
                or "\\" in name
            ):
                raise DraftWorkflowError(
                    f"ComfyUI Qwen-Image {label} is not allowlisted: {name}"
                )
        return (
            config.qwen_image_diffusion_model,
            config.qwen_image_text_encoder,
            config.qwen_image_vae,
        )

    def _qwen_image_models_installed(self) -> bool:
        if not self._comfyui_config.qwen_image_enabled:
            return False
        diffusion, encoder, vae = self._validate_qwen_image_models()
        root = self._comfyui_config.models_path
        return all(
            os.path.isfile(os.path.join(root, folder, name))
            for folder, name in (
                ("diffusion_models", diffusion),
                ("text_encoders", encoder),
                ("vae", vae),
            )
        )

    def _should_use_qwen_image(self, request: GenerationRequest) -> bool:
        if request.preset_id == "qwen-image":
            return True
        source_prompt = request.source_prompt or request.prompt
        return bool(re.search(r"[\u3400-\u9fff]", source_prompt)) and (
            self._qwen_image_models_installed()
        )

    async def _upload_image(self, image_data: bytes, filename: str) -> str:
        import httpx

        url = f"{self._comfyui_config.server_url}/upload/image"
        async with httpx.AsyncClient(
            timeout=self._comfyui_config.connect_timeout
        ) as client:
            response = await client.post(
                url,
                files={"image": (filename, image_data, "image/png")},
                data={"type": "input", "overwrite": "true"},
            )
            response.raise_for_status()
            data = response.json()
        stored_name = data.get("name") or filename
        if not isinstance(stored_name, str) or not stored_name:
            raise DraftWorkflowError("ComfyUI image upload returned no filename")
        return stored_name

    async def _submit_workflow(
        self,
        workflow_json: dict,
        *,
        client_id: str | None = None,
    ) -> str:
        """提交工作流到 ComfyUI.

        通过 POST /prompt 提交工作流 JSON，返回 prompt_id。

        Args:
            workflow_json: ComfyUI 标准格式工作流 JSON

        Returns:
            ComfyUI 返回的 prompt_id

        Raises:
            DraftWorkflowError: 提交失败
        """
        import httpx

        url = f"{self._comfyui_config.server_url}/prompt"
        payload: dict[str, Any] = {"prompt": workflow_json}
        if client_id:
            payload["client_id"] = client_id

        async with httpx.AsyncClient(
            timeout=self._comfyui_config.connect_timeout
        ) as client:
            response = await client.post(url, json=payload)
            if response.status_code != 200:
                raise DraftWorkflowError(
                    f"ComfyUI workflow submission failed: status={response.status_code}"
                )
            data = response.json()
            prompt_id = data.get("prompt_id")
            if not prompt_id:
                raise DraftWorkflowError(
                    "ComfyUI 未返回 prompt_id"
                )
            logger.info(
                "generation_optimization.draft_generator.workflow_submitted",
                extra={"prompt_id": prompt_id},
            )
            return prompt_id

    async def _record_comfy_job(
        self, draft_id: str | None, prompt_id: str, stage: str
    ) -> None:
        if not draft_id:
            return
        draft = await self._load_draft(draft_id)
        if draft is None:
            return
        draft.comfy_prompt_id = prompt_id
        draft.stage = stage
        draft.status = (
            DRAFT_STATUS_REFINING if stage == "refining" else DRAFT_STATUS_RUNNING
        )
        draft.progress = 0.35 if stage == "refining" else 0.15
        draft.generation_params["progress_source"] = "stage"
        await self._store_draft(
            draft, max(1, int(draft.expires_at - time.time()))
        )
        await self._emit_draft_trace(
            str(draft.generation_params.get("trace_id") or ""),
            "comfyui.workflow_submitted",
            payload={
                "draft_id": draft_id,
                "prompt_id": prompt_id,
                "stage": stage,
            },
            stage="comfyui",
        )

    def _comfy_client_id(self, draft_id: str | None, stage: str) -> str:
        safe_draft = draft_id or uuid.uuid4().hex
        return f"aigateway-{safe_draft}-{stage}-{uuid.uuid4().hex[:8]}"

    def _comfy_ws_url(self, client_id: str) -> str:
        parsed = urlparse(self._comfyui_config.server_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        path = parsed.path.rstrip("/") + "/ws"
        return urlunparse(
            (scheme, parsed.netloc, path, "", f"clientId={quote(client_id, safe='')}", "")
        )

    async def _watch_comfyui_progress(
        self,
        prompt_id: str,
        *,
        draft_id: str | None,
        trace_id: str | None,
        client_id: str,
        stage: str,
    ) -> None:
        """Consume ComfyUI WebSocket progress events and persist real step ratio."""
        if not draft_id:
            return
        try:
            import websockets
        except ImportError:
            logger.warning("ComfyUI progress websocket dependency is not installed")
            return

        last_ratio = -1.0
        try:
            async with websockets.connect(
                self._comfy_ws_url(client_id),
                open_timeout=self._comfyui_config.connect_timeout,
                ping_interval=None,
            ) as websocket:
                async for raw in websocket:
                    try:
                        message = json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if not isinstance(message, dict):
                        continue
                    data = message.get("data")
                    if not isinstance(data, dict):
                        continue
                    msg_prompt_id = data.get("prompt_id")
                    if msg_prompt_id and msg_prompt_id != prompt_id:
                        continue
                    message_type = message.get("type")
                    if message_type == "executing" and data.get("node") is None:
                        break
                    if message_type == "progress_state":
                        node_states = data.get("nodes")
                        if not isinstance(node_states, dict):
                            continue
                        running_states = [
                            node_state
                            for node_state in node_states.values()
                            if isinstance(node_state, dict)
                            and node_state.get("prompt_id") == prompt_id
                            and isinstance(node_state.get("value"), (int, float))
                            and isinstance(node_state.get("max"), (int, float))
                            and float(node_state.get("max") or 0) > 0
                        ]
                        if not running_states:
                            continue
                        data = max(
                            running_states,
                            key=lambda node_state: float(node_state.get("max") or 0),
                        )
                    elif message_type != "progress":
                        continue
                    value = data.get("value")
                    max_value = data.get("max")
                    if not isinstance(value, (int, float)) or not isinstance(
                        max_value, (int, float)
                    ) or max_value <= 0:
                        continue
                    ratio = min(0.99, max(0.0, float(value) / float(max_value)))
                    if ratio - last_ratio < 0.01 and ratio < 0.99:
                        continue
                    last_ratio = ratio
                    await self._apply_comfyui_progress(
                        draft_id,
                        prompt_id,
                        ratio,
                        value=int(value),
                        max_value=int(max_value),
                        stage=stage,
                        trace_id=trace_id,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(
                "ComfyUI progress websocket ended: prompt_id=%s error=%s",
                prompt_id,
                type(exc).__name__,
            )

    async def _apply_comfyui_progress(
        self,
        draft_id: str,
        prompt_id: str,
        ratio: float,
        *,
        value: int,
        max_value: int,
        stage: str,
        trace_id: str | None,
    ) -> None:
        draft = await self._load_draft(draft_id)
        if draft is None or draft.comfy_prompt_id != prompt_id:
            return
        if draft.status not in {
            DRAFT_STATUS_GENERATING,
            DRAFT_STATUS_QUEUED,
            DRAFT_STATUS_RUNNING,
            DRAFT_STATUS_REFINING,
        }:
            return
        base = 0.35 if stage == "refining" else 0.10
        span = 0.60 if stage == "refining" else 0.85
        draft.progress = min(0.99, base + ratio * span)
        draft.stage = f"sampling {value}/{max_value}"
        draft.generation_params["progress_source"] = "comfyui"
        await self._store_draft(draft, max(1, int(draft.expires_at - time.time())))
        await self._emit_draft_trace(
            trace_id,
            "comfyui.progress",
            payload={
                "draft_id": draft_id,
                "prompt_id": prompt_id,
                "value": value,
                "max": max_value,
                "progress": round(draft.progress, 4),
            },
            stage="comfyui",
        )

    async def _poll_results(
        self,
        prompt_id: str,
        timeout: int | None = None,
        *,
        trace_id: str | None = None,
        draft_id: str | None = None,
        progress_client_id: str | None = None,
        progress_stage: str = "running",
    ) -> list[bytes]:
        """轮询 ComfyUI 获取工作流执行结果.

        通过 GET /history/{prompt_id} 轮询直到工作流完成，
        然后获取输出图片数据。

        Args:
            prompt_id: 工作流提交返回的 prompt_id
            timeout: 超时时间/秒，默认使用 comfyui_config.execution_timeout

        Returns:
            ComfyUI 的全部媒体输出，保持节点与批次顺序。

        Raises:
            DraftWorkflowError: 轮询超时或获取结果失败
        """
        import httpx

        if timeout is None:
            timeout = self._comfyui_config.execution_timeout

        history_url = f"{self._comfyui_config.server_url}/history/{prompt_id}"
        poll_interval = 1.0  # seconds
        deadline = time.monotonic() + timeout
        transient_errors = 0
        progress_task: asyncio.Task[None] | None = None
        if progress_client_id and draft_id:
            progress_task = asyncio.create_task(
                self._watch_comfyui_progress(
                    prompt_id,
                    draft_id=draft_id,
                    trace_id=trace_id,
                    client_id=progress_client_id,
                    stage=progress_stage,
                )
            )

        async def sleep_until_next_poll() -> None:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(poll_interval, remaining))

        try:
            async with httpx.AsyncClient(
                timeout=self._comfyui_config.connect_timeout
            ) as client:
                while time.monotonic() < deadline:
                    try:
                        response = await client.get(history_url)
                    except (httpx.TimeoutException, httpx.TransportError) as exc:
                        transient_errors += 1
                        if transient_errors == 1 or transient_errors % 10 == 0:
                            logger.warning(
                                "generation_optimization.draft_generator.poll_transient_error",
                                extra={
                                    "prompt_id": prompt_id,
                                    "error_type": type(exc).__name__,
                                    "attempts": transient_errors,
                                },
                            )
                            await self._emit_draft_trace(
                                trace_id,
                                "comfyui.poll_transient_error",
                                status="error",
                                payload={
                                    "draft_id": draft_id,
                                    "prompt_id": prompt_id,
                                    "error_type": type(exc).__name__,
                                    "attempts": transient_errors,
                                },
                                stage="comfyui",
                            )
                        await sleep_until_next_poll()
                        continue
                    if response.status_code == 200:
                        history = response.json()
                        if prompt_id in history:
                            # Workflow completed — extract output image
                            prompt_data = history[prompt_id]
                            status_messages = prompt_data.get("status", {}).get(
                                "messages", []
                            )
                            for message in status_messages:
                                if not isinstance(message, (list, tuple)):
                                    continue
                                message_text = json.dumps(
                                    message, ensure_ascii=False, default=str
                                ).lower()
                                if (
                                    "out of memory" in message_text
                                    or "cuda error: memory" in message_text
                                ):
                                    raise DraftWorkflowError(
                                        "comfyui_gpu_out_of_memory"
                                    )
                                if message and message[0] == "execution_error":
                                    raise DraftWorkflowError(
                                        "comfyui_workflow_execution_failed"
                                    )
                            outputs = prompt_data.get("outputs", {})
                            results: list[bytes] = []
                            for node_output in outputs.values():
                                media_entries: list[dict[str, Any]] = []
                                for output_key in (
                                    "images",
                                    "videos",
                                    "video",
                                    "gifs",
                                    "audio",
                                ):
                                    value = node_output.get(output_key, [])
                                    if isinstance(value, dict):
                                        media_entries.append(value)
                                    elif isinstance(value, list):
                                        media_entries.extend(
                                            entry
                                            for entry in value
                                            if isinstance(entry, dict)
                                        )
                                download_deferred = False
                                for media_info in media_entries:
                                    filename = media_info.get("filename", "")
                                    if not filename:
                                        continue
                                    subfolder = media_info.get("subfolder", "")
                                    media_type = media_info.get("type", "output")
                                    view_url = (
                                        f"{self._comfyui_config.server_url}/view"
                                        f"?filename={filename}"
                                        f"&subfolder={subfolder}"
                                        f"&type={media_type}"
                                    )
                                    try:
                                        media_response = await client.get(view_url)
                                    except (
                                        httpx.TimeoutException,
                                        httpx.TransportError,
                                    ) as exc:
                                        download_deferred = True
                                        logger.warning(
                                            "generation_optimization.draft_generator.media_download_transient_error",
                                            extra={
                                                "prompt_id": prompt_id,
                                                "output_filename": filename,
                                                "error_type": type(exc).__name__,
                                            },
                                        )
                                        await self._emit_draft_trace(
                                            trace_id,
                                            "comfyui.media_download_transient_error",
                                            status="error",
                                            payload={
                                                "draft_id": draft_id,
                                                "prompt_id": prompt_id,
                                                "output_filename": filename,
                                                "error_type": type(exc).__name__,
                                            },
                                            stage="comfyui",
                                        )
                                        continue
                                    if media_response.status_code == 200:
                                        logger.info(
                                            "generation_optimization.draft_generator.result_received",
                                            extra={
                                                "prompt_id": prompt_id,
                                                "output_filename": filename,
                                            },
                                        )
                                        results.append(media_response.content)
                                        await self._emit_draft_trace(
                                            trace_id,
                                            "comfyui.media_downloaded",
                                            payload={
                                                "draft_id": draft_id,
                                                "prompt_id": prompt_id,
                                                "output_filename": filename,
                                                "bytes": len(media_response.content),
                                            },
                                            stage="comfyui",
                                        )
                            if results:
                                await self._emit_draft_trace(
                                    trace_id,
                                    "comfyui.workflow_completed",
                                    payload={
                                        "draft_id": draft_id,
                                        "prompt_id": prompt_id,
                                        "outputs": len(results),
                                    },
                                    stage="comfyui",
                                )
                                return results
                            if download_deferred:
                                await sleep_until_next_poll()
                                continue
                            raise DraftWorkflowError(
                                "ComfyUI workflow completed without media output: "
                                f"prompt_id={prompt_id}"
                            )

                    await sleep_until_next_poll()
        except asyncio.CancelledError:
            await asyncio.shield(self._cancel_comfyui_workflow(prompt_id))
            raise
        finally:
            if progress_task is not None and not progress_task.done():
                progress_task.cancel()
                await asyncio.gather(progress_task, return_exceptions=True)

        await self._cancel_comfyui_workflow(prompt_id)
        raise DraftWorkflowError(
            f"ComfyUI 工作流执行超时 ({timeout}s): prompt_id={prompt_id}"
        )

    async def _cancel_comfyui_workflow(self, prompt_id: str) -> None:
        """Cancel only the matching pending/running ComfyUI workflow."""
        import httpx

        base_url = self._comfyui_config.server_url
        try:
            async with httpx.AsyncClient(
                timeout=self._comfyui_config.connect_timeout
            ) as client:
                response = await client.get(f"{base_url}/queue")
                if response.status_code != 200:
                    return
                queue = response.json()
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
                elif prompt_id in running_ids:
                    await client.post(f"{base_url}/interrupt")
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            logger.warning(
                "Failed to cancel ComfyUI workflow %s: %s",
                prompt_id,
                type(exc).__name__,
            )

    async def _poll_result(
        self,
        prompt_id: str,
        timeout: int | None = None,
        *,
        trace_id: str | None = None,
        draft_id: str | None = None,
        progress_client_id: str | None = None,
        progress_stage: str = "running",
    ) -> bytes:
        """Backward-compatible first-output wrapper."""
        return (
            await self._poll_results(
                prompt_id,
                timeout,
                trace_id=trace_id,
                draft_id=draft_id,
                progress_client_id=progress_client_id,
                progress_stage=progress_stage,
            )
        )[0]

    # ===================================================================
    # ComfyUI 工作流 JSON 构建器
    # ===================================================================

    def _build_image_draft_workflow(
        self,
        request: GenerationRequest,
        config: DraftWorkflowConfig | None = None,
        *,
        seed: int | None = None,
    ) -> dict:
        """构建低分辨率图片生成工作流 JSON.

        工作流包含:
        - CheckpointLoaderSimple: 加载 SDXL base 模型
        - EmptyLatentImage: 使用 config.draft_resolution 潜空间
        - CLIPTextEncode (positive): 用户 prompt
        - CLIPTextEncode (negative): 默认负面 prompt
        - KSampler: 采样器节点
        - VAEDecode: 解码潜空间为图片
        - SaveImage: 保存输出

        Args:
            request: 生成请求
            config: Draft 工作流配置（可选，默认使用 self._config）

        Returns:
            ComfyUI 标准格式工作流 JSON dict

        需求: 4.2
        """
        cfg = config or self._config
        if self._should_use_qwen_image(request):
            return self._build_qwen_image_workflow(request, cfg, seed=seed)
        prompt_text = request.prompt or "a beautiful image"
        draft_w, draft_h = cfg.draft_resolution

        workflow: dict = {
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": seed if seed is not None else int(uuid.uuid4().int % (2**32)),
                    "steps": 12,
                    "cfg": 7.5,
                    "sampler_name": "euler",
                    "scheduler": "normal",
                    "denoise": 1.0,
                    "model": ["4", 0],
                    "positive": ["6", 0],
                    "negative": ["7", 0],
                    "latent_image": ["5", 0],
                },
            },
            "4": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {
                    "ckpt_name": self._validate_checkpoint(),
                },
            },
            "5": {
                "class_type": "EmptyLatentImage",
                "inputs": {
                    "width": draft_w,
                    "height": draft_h,
                    "batch_size": 1,
                },
            },
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "text": prompt_text,
                    "clip": ["4", 1],
                },
            },
            "7": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "text": _DEFAULT_NEGATIVE_PROMPT,
                    "clip": ["4", 1],
                },
            },
            "8": {
                "class_type": "VAEDecode",
                "inputs": {
                    "samples": ["3", 0],
                    "vae": ["4", 2],
                },
            },
            "9": {
                "class_type": "SaveImage",
                "inputs": {
                    "filename_prefix": f"draft_{request.request_id}",
                    "images": ["8", 0],
                },
            },
        }

        return workflow

    def _build_qwen_image_workflow(
        self,
        request: GenerationRequest,
        config: DraftWorkflowConfig,
        *,
        seed: int | None,
    ) -> dict:
        """Build the official Core-node Qwen-Image text-to-image graph."""
        diffusion, encoder, vae = self._validate_qwen_image_models()
        width, height = config.draft_resolution
        max_edge = max(256, int(self._comfyui_config.qwen_image_max_draft_edge))
        scale = min(1.0, max_edge / max(width, height))
        width = max(256, (int(width * scale) // 16) * 16)
        height = max(256, (int(height * scale) // 16) * 16)
        steps = max(1, int(self._comfyui_config.qwen_image_draft_steps))
        prompt = request.source_prompt or request.prompt or "一幅精美的图片"
        return {
            "1": {
                "class_type": "VAELoader",
                "inputs": {"vae_name": vae},
            },
            "2": {
                "class_type": "CLIPLoader",
                "inputs": {
                    "clip_name": encoder,
                    "type": "qwen_image",
                    "device": "default",
                },
            },
            "3": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "", "clip": ["2", 0]},
            },
            "4": {
                "class_type": "UNETLoader",
                "inputs": {
                    "unet_name": diffusion,
                    "weight_dtype": "default",
                },
            },
            "5": {
                "class_type": "ModelSamplingAuraFlow",
                "inputs": {"shift": 3.1, "model": ["4", 0]},
            },
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": prompt, "clip": ["2", 0]},
            },
            "7": {
                "class_type": "EmptySD3LatentImage",
                "inputs": {
                    "width": width,
                    "height": height,
                    "batch_size": 1,
                },
            },
            "8": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": seed if seed is not None else int(uuid.uuid4().int % (2**32)),
                    "steps": steps,
                    "cfg": 4.0,
                    "sampler_name": "euler",
                    "scheduler": "simple",
                    "denoise": 1.0,
                    "model": ["5", 0],
                    "positive": ["6", 0],
                    "negative": ["3", 0],
                    "latent_image": ["7", 0],
                },
            },
            "9": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["8", 0], "vae": ["1", 0]},
            },
            "10": {
                "class_type": "SaveImage",
                "inputs": {
                    "filename_prefix": f"qwen_image_{request.request_id}",
                    "images": ["9", 0],
                },
            },
        }

    def _build_refine_workflow(
        self,
        input_name: str,
        prompt: str,
        seed: int,
        target_resolution: tuple[int, int],
    ) -> dict:
        """Build same-checkpoint img2img refinement from the approved draft."""
        target_width, target_height = target_resolution

        workflow: dict = {
            "1": {
                "class_type": "LoadImage",
                "inputs": {"image": input_name},
            },
            "2": {
                "class_type": "ImageScale",
                "inputs": {
                    "image": ["1", 0],
                    "upscale_method": "lanczos",
                    "width": target_width,
                    "height": target_height,
                    "crop": "disabled",
                },
            },
            "3": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": self._validate_checkpoint()},
            },
            "4": {
                "class_type": "VAEEncode",
                "inputs": {"pixels": ["2", 0], "vae": ["3", 2]},
            },
            "5": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": prompt, "clip": ["3", 1]},
            },
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": _DEFAULT_NEGATIVE_PROMPT, "clip": ["3", 1]},
            },
            "7": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": seed,
                    "steps": 24,
                    "cfg": 7.0,
                    "sampler_name": "euler",
                    "scheduler": "normal",
                    "denoise": 0.25,
                    "model": ["3", 0],
                    "positive": ["5", 0],
                    "negative": ["6", 0],
                    "latent_image": ["4", 0],
                },
            },
            "8": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["7", 0], "vae": ["3", 2]},
            },
            "9": {
                "class_type": "SaveImage",
                "inputs": {
                    "filename_prefix": "refined",
                    "images": ["8", 0],
                },
            },
        }

        return workflow

    def _validate_upscale_model(self) -> str:
        """Return an approved basename-only upscaler model."""
        config = self._comfyui_config
        name = config.upscale_model
        if (
            not config.upscale_enabled
            or name not in set(config.allowed_upscale_models)
            or not _SAFE_PATH_COMPONENT.fullmatch(name)
            or "/" in name
            or "\\" in name
        ):
            raise DraftWorkflowError(
                f"ComfyUI upscale model is not enabled or allowlisted: {name}"
            )
        return name

    @staticmethod
    def _read_image_resolution(image_data: bytes) -> tuple[int, int]:
        try:
            with Image.open(BytesIO(image_data)) as image:
                width, height = image.size
        except Exception as exc:
            raise DraftWorkflowError("ComfyUI returned an invalid image") from exc
        if width < 1 or height < 1:
            raise DraftWorkflowError("ComfyUI returned an image with invalid dimensions")
        return width, height

    @classmethod
    def _reported_image_resolution(
        cls,
        image_data: bytes,
        fallback: tuple[int, int],
    ) -> tuple[int, int]:
        """Use decoded output dimensions; retain a fallback for legacy/mock payloads."""
        try:
            return cls._read_image_resolution(image_data)
        except DraftWorkflowError:
            logger.warning("unable to decode confirmed image dimensions; using bounded target")
            return fallback

    def _faithful_upscale_resolution(self, image_data: bytes) -> tuple[int, int]:
        width, height = self._read_image_resolution(image_data)
        max_edge = self._comfyui_config.max_upscale_long_edge
        scale = max(1.0, max_edge / max(width, height))
        return max(1, round(width * scale)), max(1, round(height * scale))

    def _build_faithful_upscale_workflow(
        self,
        input_name: str,
        target_resolution: tuple[int, int],
    ) -> dict:
        """Build a Core-node-only RealESRGAN workflow without diffusion denoise."""
        target_width, target_height = target_resolution
        return {
            "1": {
                "class_type": "LoadImage",
                "inputs": {"image": input_name},
            },
            "2": {
                "class_type": "UpscaleModelLoader",
                "inputs": {"model_name": self._validate_upscale_model()},
            },
            "3": {
                "class_type": "ImageUpscaleWithModel",
                "inputs": {
                    "upscale_model": ["2", 0],
                    "image": ["1", 0],
                },
            },
            "4": {
                "class_type": "ImageScale",
                "inputs": {
                    "image": ["3", 0],
                    "upscale_method": "lanczos",
                    "width": target_width,
                    "height": target_height,
                    "crop": "disabled",
                },
            },
            "5": {
                "class_type": "SaveImage",
                "inputs": {
                    "filename_prefix": "faithful_4k",
                    "images": ["4", 0],
                },
            },
        }

    def _validate_video_models(self) -> tuple[str, str, str]:
        """Return the approved Wan model files and reject path/model injection."""
        config = self._comfyui_config
        approved = (
            (
                config.video_diffusion_model,
                set(config.allowed_video_diffusion_models),
                "diffusion model",
            ),
            (
                config.video_text_encoder,
                set(config.allowed_video_text_encoders),
                "text encoder",
            ),
            (config.video_vae, set(config.allowed_video_vaes), "VAE"),
        )
        for name, allowlist, label in approved:
            if name not in allowlist or "/" in name or "\\" in name:
                raise DraftWorkflowError(
                    f"ComfyUI video {label} is not allowlisted: {name}"
                )
        return (
            config.video_diffusion_model,
            config.video_text_encoder,
            config.video_vae,
        )

    def _build_video_workflow(
        self,
        *,
        input_name: str,
        prompt: str,
        seed: int,
        draft_id: str,
    ) -> dict:
        """Build the native ComfyUI Wan2.2 TI2V API workflow."""
        config = self._comfyui_config
        diffusion_model, text_encoder, vae = self._validate_video_models()
        return {
            "1": {
                "class_type": "UNETLoader",
                "inputs": {
                    "unet_name": diffusion_model,
                    "weight_dtype": "default",
                },
            },
            "2": {
                "class_type": "CLIPLoader",
                "inputs": {
                    "clip_name": text_encoder,
                    "type": "wan",
                    "device": "default",
                },
            },
            "3": {
                "class_type": "VAELoader",
                "inputs": {"vae_name": vae},
            },
            "4": {
                "class_type": "LoadImage",
                "inputs": {"image": input_name},
            },
            "5": {
                "class_type": "Wan22ImageToVideoLatent",
                "inputs": {
                    "vae": ["3", 0],
                    "start_image": ["4", 0],
                    "width": config.video_width,
                    "height": config.video_height,
                    "length": config.video_frames,
                    "batch_size": 1,
                },
            },
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": prompt, "clip": ["2", 0]},
            },
            "7": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "text": _DEFAULT_NEGATIVE_PROMPT,
                    "clip": ["2", 0],
                },
            },
            "8": {
                "class_type": "ModelSamplingSD3",
                "inputs": {"model": ["1", 0], "shift": config.video_shift},
            },
            "9": {
                "class_type": "KSampler",
                "inputs": {
                    "model": ["8", 0],
                    "seed": seed,
                    "steps": config.video_steps,
                    "cfg": config.video_cfg,
                    "sampler_name": "uni_pc",
                    "scheduler": "simple",
                    "positive": ["6", 0],
                    "negative": ["7", 0],
                    "latent_image": ["5", 0],
                    "denoise": 1.0,
                },
            },
            "10": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["9", 0], "vae": ["3", 0]},
            },
            "11": {
                "class_type": "CreateVideo",
                "inputs": {"images": ["10", 0], "fps": config.video_fps},
            },
            "12": {
                "class_type": "SaveVideo",
                "inputs": {
                    "video": ["11", 0],
                    "filename_prefix": f"video_{draft_id}",
                    "format": "mp4",
                    "codec": "h264",
                },
            },
        }

    # ===================================================================
    # 内部方法 — ComfyUI 集成预览生成
    # ===================================================================

    async def _generate_image_preview_with_comfyui(
        self,
        request: GenerationRequest,
        config: DraftWorkflowConfig,
        seed: int | None = None,
        draft_id: str | None = None,
    ) -> bytes:
        """Generate a low-cost image draft through the required ComfyUI backend."""
        await self._check_comfyui()
        await self._ensure_storage_capacity()
        async with self._comfyui_semaphore:
            try:
                workflow = self._build_image_draft_workflow(
                    request, config, seed=seed
                )
                client_id = self._comfy_client_id(draft_id, "draft")
                prompt_id = await self._submit_workflow(
                    workflow, client_id=client_id
                )
                await self._record_comfy_job(draft_id, prompt_id, "running")
                image_data = await self._poll_result(
                    prompt_id,
                    trace_id=request.trace_id,
                    draft_id=draft_id,
                    progress_client_id=client_id,
                    progress_stage="running",
                )
                logger.info(
                    "generation_optimization.draft_generator.comfyui_image_preview",
                    extra={"request_id": request.request_id, "size": len(image_data)},
                )
                return image_data
            except Exception as exc:
                if isinstance(exc, DraftWorkflowError):
                    raise
                raise DraftWorkflowError("ComfyUI image draft failed") from exc

    async def _generate_video_previews_with_comfyui(
        self,
        request: GenerationRequest,
        config: DraftWorkflowConfig,
        *,
        seed: int,
        draft_id: str,
    ) -> list[bytes]:
        """Generate one cheap SDXL keyframe; Wan runs only after confirmation."""
        preview = await self._generate_image_preview_with_comfyui(
            request,
            config,
            seed=seed,
            draft_id=draft_id,
        )
        return [preview]

    async def _generate_video_with_comfyui(self, draft: DraftResult) -> bytes:
        """Generate MP4 from the approved keyframe using local ComfyUI only."""
        if not self._comfyui_config.video_enabled:
            raise DraftWorkflowError("comfyui_video_not_enabled")
        if not draft.previews:
            raise DraftWorkflowError("Video draft has no approved keyframe")
        await self._ensure_storage_capacity()
        input_name = await self._upload_image(
            draft.previews[0], f"video-keyframe-{draft.draft_id}.png"
        )
        workflow = self._build_video_workflow(
            input_name=input_name,
            prompt=str(draft.generation_params.get("prompt", "")),
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
        logger.info(
            "generation_optimization.draft_generator.comfyui_video_completed",
            extra={
                "draft_id": draft.draft_id,
                "workflow_version": self._comfyui_config.video_workflow_version,
                "output_size": len(result),
            },
        )
        return result

    async def _upscale_with_comfyui(
        self,
        draft: DraftResult,
        target_resolution: tuple[int, int],
    ) -> bytes:
        """通过 ComfyUI 执行高清精修.

        Args:
            draft: 草图结果
            target_resolution: 目标分辨率

        Returns:
            精修后的 bytes 数据
        """
        try:
            # Use the first preview as input for upscale. Confirming a draft
            # first moves the metadata to ``refining``; the preview itself is
            # persisted on disk and may need to be loaded lazily after that
            # state transition.
            draft_data = draft.previews[0] if draft.previews else b""
            if not draft_data:
                draft_dir = self._draft_dir(draft.session_id, draft.draft_id)
                draft.previews = self._read_preview_bytes(draft_dir, draft.media_type)
                draft_data = draft.previews[0] if draft.previews else b""
            if not draft_data:
                raise DraftWorkflowError("Draft has no preview image to refine")

            await self._ensure_storage_capacity()
            input_name = await self._upload_image(
                draft_data, f"draft-{draft.draft_id}.png"
            )
            quality = str(draft.generation_params.get("quality", "standard"))
            if quality == "faithful_4k":
                target_resolution = self._faithful_upscale_resolution(draft_data)
                workflow = self._build_faithful_upscale_workflow(
                    input_name=input_name,
                    target_resolution=target_resolution,
                )
            else:
                workflow = self._build_refine_workflow(
                    input_name=input_name,
                    prompt=str(draft.generation_params.get("prompt", "")),
                    seed=int(draft.generation_params.get("seed", 0)),
                    target_resolution=target_resolution,
                )
            async with self._comfyui_semaphore:
                client_id = self._comfy_client_id(draft.draft_id, "refine")
                prompt_id = await self._submit_workflow(
                    workflow, client_id=client_id
                )
                draft.comfy_prompt_id = prompt_id
                draft.stage = "refining"
                draft.progress = 0.35
                draft.generation_params["progress_source"] = "stage"
                await self._store_draft(
                    draft, max(1, int(draft.expires_at - time.time()))
                )
                await self._emit_draft_trace(
                    str(draft.generation_params.get("trace_id") or ""),
                    "comfyui.workflow_submitted",
                    payload={
                        "draft_id": draft.draft_id,
                        "prompt_id": prompt_id,
                        "stage": "refining",
                    },
                    stage="comfyui",
                )
                result_data = await self._poll_result(
                    prompt_id,
                    trace_id=str(draft.generation_params.get("trace_id") or ""),
                    draft_id=draft.draft_id,
                    progress_client_id=client_id,
                    progress_stage="refining",
                )
            logger.info(
                "generation_optimization.draft_generator.comfyui_upscale",
                extra={
                    "draft_id": draft.draft_id,
                    "target_resolution": target_resolution,
                },
            )
            return result_data
        except Exception as exc:
            if isinstance(exc, DraftWorkflowError):
                raise
            raise DraftWorkflowError("ComfyUI image refinement failed") from exc

    # ===================================================================
    # 内部方法 — 关键帧计算
    # ===================================================================

    def _is_video_request(self, request: GenerationRequest) -> bool:
        """判断请求是否为视频生成请求.

        基于 request.media_type 字段判断，默认 "image"。

        Args:
            request: 生成请求

        Returns:
            True 如果是视频请求
        """
        return request.media_type == "video"

    def _calculate_keyframe_count(
        self,
        request: GenerationRequest,
        config: DraftWorkflowConfig,
        explicit_count: int | None = None,
    ) -> int:
        """计算视频关键帧数量.

        规则:
        - 如果 explicit_count 显式指定，使用该值（最少 2）
        - 否则: max(2, ceil(duration / interval))
        - duration 来自 config.preview_video_duration_seconds
        - interval 来自 config.preview_keyframe_interval_seconds

        Args:
            request: 生成请求
            config: Draft 工作流配置
            explicit_count: 用户显式指定的数量

        Returns:
            关键帧数量，至少 2
        """
        if explicit_count is not None:
            return max(2, explicit_count)

        duration = config.preview_video_duration_seconds
        interval = config.preview_keyframe_interval_seconds

        return max(2, math.ceil(duration / interval))

    # ===================================================================
    # 内部方法 — 预览生成（占位实现）
    # ===================================================================

    async def _generate_image_preview(
        self,
        request: GenerationRequest,
        config: DraftWorkflowConfig,
    ) -> bytes:
        """生成图片预览数据.

        如果 litellm_bridge 已绑定（Agnes），通过 Agnes Images API 生成低分辨率预览（1K）。
        否则回退到占位数据。

        Args:
            request: 生成请求
            config: Draft 工作流配置

        Returns:
            预览图的 bytes 数据
        """
        width, height = config.draft_resolution
        # 草稿预览模型从配置读取（generation_optimization.draft_workflow.draft_model），
        # 避免硬编码模型名导致重命名/下线时静默回退占位。
        draft_model = getattr(config, "draft_model", "agnes-image-2.1-flash")

        # 优先使用 litellm_bridge 调 Agnes 生成真实低分辨率预览
        if self._litellm_bridge is not None:
            try:
                result = await self._litellm_bridge._do_image_generation(
                    prompt=request.prompt,
                    model=draft_model,
                    size=f"{width}x{height}",
                    response_format="url",
                )
                # _do_image_generation 归一为 chat completions 格式:
                # choices[0].message.content = URL 或 b64_json
                url = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                if url:
                    return await self._fetch_image_bytes(url)
            except Exception as exc:
                logger.warning("Agnes 低分辨率预览生成失败，回退到占位: %s", exc)

        # Placeholder: generate a minimal PNG-like header + content indicator
        # In production, this calls the actual generation model at draft resolution
        # NOTE: prompt content intentionally omitted to prevent PII/secrets leakage
        # through preview image bytes returned to any caller.
        placeholder = (
            f"DRAFT_PREVIEW:image:{width}x{height}:"
            f"id={request.request_id}"
        ).encode()
        return placeholder

    async def _fetch_image_bytes(self, url: str) -> bytes:
        """下载图片并返回 bytes.

        仅处理 http(s) URL；若 content 是 b64_json（response_format 改动导致），
        返回空串触发上层回退占位，而不是把 base64 字符串当 URL 去请求。

        SSRF 防护：解析 hostname 后检查是否落在私有/内网 IP 范围，拒绝访问
        元数据端点、localhost、RFC1918 地址等。
        """
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError(f"非 HTTP 图片内容，无法下载: {str(url)[:40]!r}")

        import socket
        from urllib.parse import urlparse

        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            raise ValueError(f"无法解析 URL hostname: {url[:60]!r}")

        # Resolve hostname and reject private/internal IPs before issuing request.
        try:
            addr_infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror as exc:
            raise ValueError(f"DNS resolution failed for {hostname}: {exc}") from exc

        for family, _socktype, _proto, _canonname, sockaddr in addr_infos:
            ip = sockaddr[0]
            # IPv4
            if family == socket.AF_INET:
                socket.inet_aton(ip)
                # 127.0.0.0/8 loopback
                if ip.startswith("127.") or ip == "0.0.0.0":
                    raise ValueError(f"禁止访问 loopback 地址: {ip}")
                # 10.0.0.0/8
                if ip.startswith("10."):
                    raise ValueError(f"禁止访问 RFC1918 私有地址: {ip}")
                # 172.16.0.0/12
                parts = list(map(int, ip.split(".")))
                if parts[0] == 172 and 16 <= parts[1] <= 31:
                    raise ValueError(f"禁止访问 RFC1918 私有地址: {ip}")
                # 192.168.0.0/16
                if parts[0] == 192 and parts[1] == 168:
                    raise ValueError(f"禁止访问 RFC1918 私有地址: {ip}")
                # 169.254.0.0/16 link-local (metadata endpoints)
                if parts[0] == 169 and parts[1] == 254:
                    raise ValueError(f"禁止访问 link-local 地址: {ip}")
                # 0.0.0.0/8
                if parts[0] == 0:
                    raise ValueError(f"禁止访问特殊地址: {ip}")
            # IPv6
            elif family == socket.AF_INET6:
                if ip == "::1" or ip.startswith(("fe80:", "fc", "fd")):
                    raise ValueError(f"禁止访问 IPv6 私有/环回地址: {ip}")
                # Check for IPv4-mapped IPv6 addresses (::ffff:x.x.x.x)
                if ip.startswith("::ffff:"):
                    mapped_ip = ip[7:]
                    parts = list(map(int, mapped_ip.split(".")))
                    if len(parts) == 4 and all(0 <= p <= 255 for p in parts):
                        if parts[0] == 127 or parts[0] == 0 or parts[0] == 10 or parts[0] == 169 or (parts[0] == 172 and 16 <= parts[1] <= 31) or (parts[0] == 192 and parts[1] == 168):
                            raise ValueError(f"禁止访问 IPv4-mapped IPv6 私有地址: {ip}")

        import httpx
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            resp = await client.get(url)
            if resp.is_redirect:
                location = resp.headers.get("location", "")
                if location:
                    # Validate redirect target recursively
                    parsed_location = urlparse(location)
                    if parsed_location.hostname:
                        try:
                            redirect_addrs = socket.getaddrinfo(parsed_location.hostname, None)
                            for _, _, _, _, sockaddr in redirect_addrs:
                                redirect_ip = sockaddr[0]
                                if redirect_ip.startswith(("127.", "10.", "169.254.", "192.168.", "::1", "fe80:", "fc", "fd")) or redirect_ip == "0.0.0.0":
                                    raise ValueError(f"禁止访问重定向目标: {location}")
                        except socket.gaierror:
                            pass
            resp.raise_for_status()
            return resp.content

    def _generate_keyframe_previews(
        self,
        request: GenerationRequest,
        config: DraftWorkflowConfig,
        num_keyframes: int,
    ) -> list[bytes]:
        """生成视频关键帧预览占位数据.

        每帧都是草稿分辨率的占位图像数据。

        Args:
            request: 生成请求
            config: Draft 工作流配置
            num_keyframes: 关键帧数量

        Returns:
            关键帧 bytes 列表
        """
        width, height = config.draft_resolution
        previews: list[bytes] = []
        for i in range(num_keyframes):
            # NOTE: prompt content intentionally omitted to prevent PII/secrets leakage
            # through preview image bytes returned to any caller.
            placeholder = (
                f"DRAFT_PREVIEW:video_keyframe:{width}x{height}:"
                f"frame={i}/{num_keyframes}:"
                f"id={request.request_id}"
            ).encode()
            previews.append(placeholder)
        return previews

    def _simulate_upscale(
        self,
        draft: DraftResult,
        target_resolution: tuple[int, int],
    ) -> bytes:
        """模拟 Upscaler 放大（占位实现）.

        实际实现应调用 super-resolution 算法将草图放大到目标分辨率。

        Args:
            draft: 草图结果
            target_resolution: 目标分辨率 (width, height)

        Returns:
            放大后的占位 bytes 数据
        """
        width, height = target_resolution
        placeholder = (
            f"UPSCALED:{width}x{height}:"
            f"algorithm={self._config.upscale_algorithm}:"
            f"draft_id={draft.draft_id}:"
            f"previews_count={len(draft.previews)}"
        ).encode()
        return placeholder

    def _get_target_resolution(
        self,
        draft: DraftResult,
    ) -> tuple[int, int]:
        """从草图的 generation_params 中获取目标分辨率.

        如果未指定，使用配置的默认值。确保不超过最大分辨率限制。

        Args:
            draft: 草图结果

        Returns:
            目标分辨率 (width, height)
        """
        target = draft.generation_params.get("target_resolution")
        if target and isinstance(target, (list, tuple)) and len(target) == 2:
            width = min(int(target[0]), self._config.max_target_resolution[0])
            height = min(int(target[1]), self._config.max_target_resolution[1])
            return (width, height)
        return self._config.default_target_resolution

    # ===================================================================
    # 内部方法 — 重新生成
    # ===================================================================

    async def _regenerate_draft(self, old_draft: DraftResult) -> DraftResult:
        """基于旧草图信息重新生成一个新草图.

        新草图获得新的 draft_id，attempt_number 递增，
        TTL 从当前时间重新计算。

        Args:
            old_draft: 被拒绝的旧草图

        Returns:
            新的 DraftResult
        """
        new_draft_id = uuid.uuid4().hex
        now = time.time()
        ttl_seconds = self._config.retention_period_hours * 3600
        expires_at = now + ttl_seconds

        # Reuse the approved workflow inputs and vary only the seed.
        media_type = old_draft.generation_params.get("media_type", "image")
        is_video = media_type == "video"
        generation_params = old_draft.generation_params.copy()
        generation_params["seed"] = int(uuid.uuid4().int % (2**32))
        request = GenerationRequest(
            prompt=str(generation_params.get("prompt", "")),
            target_resolution=tuple(
                generation_params.get(
                    "target_resolution", self._config.default_target_resolution
                )
            ),
            media_type=media_type,
            quality=str(generation_params.get("quality", "standard")),
            preset_id=generation_params.get("preset_id"),
            request_id=str(generation_params.get("request_id") or uuid.uuid4().hex),
            trace_id=str(generation_params.get("trace_id") or ""),
        )

        new_draft = DraftResult(
            draft_id=new_draft_id,
            previews=[],
            generation_params=generation_params,
            created_at=now,
            expires_at=expires_at,
            attempt_number=old_draft.attempt_number + 1,
            max_attempts=old_draft.max_attempts,
            status=DRAFT_STATUS_QUEUED,
            media_type=media_type,
            session_id=old_draft.session_id,
            user_id=old_draft.user_id,
            group_id=old_draft.group_id,
            progress=0.0,
            stage="queued",
            workflow_version=self._comfyui_config.workflow_version,
        )

        # Store new draft
        await self._store_draft(new_draft, ttl_seconds)
        bg_task = asyncio.create_task(
            self._generate_draft_async(
                draft_id=new_draft_id,
                request=request,
                config=self._config,
                keyframe_count=(
                    int(generation_params.get("explicit_keyframe_count"))
                    if generation_params.get("explicit_keyframe_count") is not None
                    else None
                ),
                is_video=is_video,
                media_type=media_type,
                generation_params=generation_params,
                ttl_seconds=ttl_seconds,
                expires_at=expires_at,
                chat_session_id=old_draft.session_id,
                user_id=old_draft.user_id,
                group_id=old_draft.group_id,
            ),
            name=f"draft-regenerate-{new_draft_id}",
        )
        self._bg_tasks.add(bg_task)
        bg_task.add_done_callback(self._bg_tasks.discard)

        logger.info(
            "generation_optimization.draft_generator.draft_regenerated",
            extra={
                "new_draft_id": new_draft_id,
                "old_draft_id": old_draft.draft_id,
                "attempt_number": new_draft.attempt_number,
                "max_attempts": new_draft.max_attempts,
            },
        )

        return new_draft

    # ===================================================================
    # 内部方法 — 双层存储（Redis 元数据 + 文件 bytes）
    # ===================================================================

    def _make_redis_key(self, draft_id: str) -> str:
        """构建 Redis 键名.

        格式: aigateway:draft:{draft_id}
        Redis 只存轻量元数据 + status；previews/result bytes 落盘文件。

        Args:
            draft_id: 草图唯一标识

        Returns:
            Redis 键名
        """
        return f"{_DRAFT_KEY_PREFIX}:{draft_id}"

    def _make_session_index_key(self, session_id: str) -> str:
        """构建 session→draft_id 集合的 Redis 键名 (供 delete_session 批量删)."""
        return f"{_DRAFT_SESSION_KEY_PREFIX}:{self._safe_path_component(session_id, 'session_id')}"

    @staticmethod
    def _safe_path_component(value: str, field_name: str) -> str:
        """Validate one filesystem path component before joining it."""
        if not isinstance(value, str) or not _SAFE_PATH_COMPONENT.fullmatch(value):
            raise DraftWorkflowError(f"invalid_{field_name}")
        return value

    def _draft_dir(self, session_id: str | None, draft_id: str) -> str:
        """草稿文件目录路径: {store_dir}/{session_id or 'unknown'}/{draft_id}/.

        仅计算路径，**不创建目录**。session_id 缺失时归入 'unknown' 桶。
        读路径（_load_draft / get_result_bytes / delete_draft）调用本函数——
        若此处 makedirs，会对 Redis key 已过期但文件已被 cleaner 删除的草稿
        重建空目录，cleaner 的 mtime 兜底要到 24h 后才回收，造成磁盘泄漏。
        需要确保目录存在的写路径请改用 _ensure_draft_dir。
        """
        sid = self._safe_path_component(session_id or "unknown", "session_id")
        safe_draft_id = self._safe_path_component(draft_id, "draft_id")
        return os.path.join(self._store_dir, sid, safe_draft_id)

    def _ensure_draft_dir(self, session_id: str | None, draft_id: str) -> str:
        """返回草稿目录路径并确保其存在（仅写路径调用）。"""
        path = self._draft_dir(session_id, draft_id)
        os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def _meta_path(draft_dir: str) -> str:
        return os.path.join(draft_dir, "meta.json")

    def _write_meta(self, draft_dir: str, draft: DraftResult) -> None:
        """写 meta.json（含 expires_at，供 DraftSessionCleaner 判过期）."""
        meta = {
            "draft_id": draft.draft_id,
            "session_id": draft.session_id,
            "user_id": draft.user_id,
            "group_id": draft.group_id,
            "media_type": draft.media_type,
            "status": draft.status,
            "expires_at": draft.expires_at,
            "created_at": draft.created_at,
            "attempt_number": draft.attempt_number,
            "max_attempts": draft.max_attempts,
            "generation_params": draft.generation_params,
            "progress": draft.progress,
            "stage": draft.stage,
            "workflow_version": draft.workflow_version,
            "comfy_prompt_id": draft.comfy_prompt_id,
            "gpu_seconds": draft.gpu_seconds,
        }
        self._write_meta_dict(draft_dir, meta)

    def _write_meta_dict(self, draft_dir: str, meta: dict[str, Any]) -> None:
        """Atomically persist an already serialized draft metadata mapping."""
        tmp = self._meta_path(draft_dir) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        os.replace(tmp, self._meta_path(draft_dir))  # 原子写，防 cleaner 读半截

    def _read_meta(self, draft_dir: str) -> dict[str, Any] | None:
        """读 meta.json；不存在/损坏返回 None."""
        path = self._meta_path(draft_dir)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError, ValueError):
            return None

    def _write_preview_bytes(
        self, draft_dir: str, previews: list[bytes], media_type: str
    ) -> None:
        """写预览 bytes: 图片单文件 preview.bin；视频多文件 preview_{i}.bin."""
        if not previews:
            return
        if media_type == "video":
            for i, p in enumerate(previews):
                with open(os.path.join(draft_dir, f"preview_{i}.bin"), "wb") as f:
                    f.write(p)
        else:
            with open(os.path.join(draft_dir, "preview.bin"), "wb") as f:
                f.write(previews[0])

    def _read_preview_bytes(
        self, draft_dir: str, media_type: str
    ) -> list[bytes]:
        """读预览 bytes. 文件缺失返回空列表."""
        result: list[bytes] = []
        if media_type == "video":
            i = 0
            while True:
                p = os.path.join(draft_dir, f"preview_{i}.bin")
                if not os.path.isfile(p):
                    break
                with open(p, "rb") as f:
                    result.append(f.read())
                i += 1
        else:
            p = os.path.join(draft_dir, "preview.bin")
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    result.append(f.read())
        return result

    def _write_result_bytes(self, draft_dir: str, data: bytes) -> None:
        """写高清结果 bytes (confirm 后持久化，修复原'高清图未存'bug)."""
        with open(os.path.join(draft_dir, "result.bin"), "wb") as f:
            f.write(data)

    def _read_result_bytes(self, draft_dir: str) -> bytes | None:
        """读高清结果 bytes；不存在返回 None."""
        p = os.path.join(draft_dir, "result.bin")
        if not os.path.isfile(p):
            return None
        with open(p, "rb") as f:
            return f.read()

    async def _index_draft_to_session(
        self, session_id: str | None, draft_id: str, ttl_seconds: int
    ) -> None:
        """把 draft_id 加入 session 索引集合（delete_session 批量删 key 用）."""
        if not session_id:
            return
        key = self._make_session_index_key(session_id)
        if self._redis_client is not None:
            try:
                await self._redis_client.sadd(key, draft_id)
                await self._redis_client.expire(key, ttl_seconds)
            except Exception as exc:
                logger.debug("session index sadd failed: %s", exc)
        else:
            self._memory_session_index.setdefault(session_id, set()).add(draft_id)

    async def _store_draft(self, draft: DraftResult, ttl_seconds: int) -> None:
        """存草图元数据到 Redis + previews bytes 到文件.

        Redis 只存轻量元数据（含 status/session_id/user_id/group_id/media_type），
        不再存 previews base64（大 value 且易被 TTL 删）。previews bytes 落盘
        {store_dir}/{session_id}/{draft_id}/preview*.bin + meta.json。

        Args:
            draft: 草图结果
            ttl_seconds: TTL 秒数
        """
        draft_dir = self._ensure_draft_dir(draft.session_id, draft.draft_id)

        # previews bytes 落盘（generating 阶段 previews 为空，仅建目录 + meta）
        if draft.previews:
            self._write_preview_bytes(draft_dir, draft.previews, draft.media_type)

        # meta.json（cleaner 据此判过期；原子写）
        self._write_meta(draft_dir, draft)

        # Redis 元数据
        serialized = {
            "draft_id": draft.draft_id,
            "session_id": draft.session_id,
            "user_id": draft.user_id,
            "group_id": draft.group_id,
            "media_type": draft.media_type,
            "generation_params": draft.generation_params,
            "created_at": draft.created_at,
            "expires_at": draft.expires_at,
            "attempt_number": draft.attempt_number,
            "max_attempts": draft.max_attempts,
            "status": draft.status,
            "video_id": draft.video_id,
            "progress": draft.progress,
            "stage": draft.stage,
            "workflow_version": draft.workflow_version,
            "comfy_prompt_id": draft.comfy_prompt_id,
            "gpu_seconds": draft.gpu_seconds,
            "error": draft.error,
            "store_dir": draft_dir,  # 供 _load_draft 定位文件
        }
        data = json.dumps(serialized)

        if self._redis_client is not None:
            await self._redis_client.set(
                self._make_redis_key(draft.draft_id),
                data,
                ex=ttl_seconds,
            )
        else:
            self._memory_store[self._make_redis_key(draft.draft_id)] = data

        await self._index_draft_to_session(draft.session_id, draft.draft_id, ttl_seconds)

    async def _load_draft(self, draft_id: str) -> DraftResult | None:
        """从 Redis 加载草图元数据; previews bytes 按需从文件懒加载.

        Args:
            draft_id: 草图唯一标识

        Returns:
            DraftResult 或 None。previews 可能为空（generating 阶段或文件丢失），
            调用方需要完整 previews 时显式调 _read_preview_bytes。
        """
        key = self._make_redis_key(draft_id)

        if self._redis_client is not None:
            raw = await self._redis_client.get(key)
        else:
            raw = self._memory_store.get(key)

        if raw is None:
            return None

        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.error(
                "generation_optimization.draft_generator.deserialize_error",
                extra={"draft_id": draft_id},
            )
            return None

        return self._draft_from_serialized(draft_id, data)

    def _draft_from_serialized(self, draft_id: str, data: dict[str, Any]) -> DraftResult:
        media_type = data.get("media_type", "image")
        draft_dir = data.get("store_dir") or self._draft_dir(data.get("session_id"), draft_id)

        # previews 懒加载：草稿预览生成前无文件，返回空列表（调用方按需再读）。
        # ``refining`` 已经发生在用户确认之后，必须继续加载已持久化的 preview，
        # 否则 confirm 路径会看不到刚生成好的草稿图。
        pre_preview_statuses = {
            DRAFT_STATUS_GENERATING,
            DRAFT_STATUS_QUEUED,
            DRAFT_STATUS_RUNNING,
        }
        previews = (
            self._read_preview_bytes(draft_dir, media_type)
            if data.get("status") not in pre_preview_statuses
            else []
        )

        return DraftResult(
            draft_id=data["draft_id"],
            previews=previews,
            generation_params=data.get("generation_params", {}),
            created_at=data.get("created_at", 0.0),
            expires_at=data.get("expires_at", 0.0),
            attempt_number=data.get("attempt_number", 1),
            max_attempts=data.get("max_attempts", self._config.max_regeneration_attempts),
            status=data.get("status", DRAFT_STATUS_PENDING),
            media_type=media_type,
            session_id=data.get("session_id"),
            user_id=data.get("user_id"),
            group_id=data.get("group_id"),
            video_id=data.get("video_id"),
            progress=float(data.get("progress", 0.0)),
            stage=data.get("stage", data.get("status", "pending")),
            workflow_version=data.get("workflow_version", ""),
            comfy_prompt_id=data.get("comfy_prompt_id"),
            gpu_seconds=float(data.get("gpu_seconds", 0.0)),
            error=data.get("error"),
        )

    async def get_result_bytes(self, draft_id: str) -> bytes:
        """读取 confirm 后的高清结果 bytes (GET /admin/draft/{id}/result 用).

        Args:
            draft_id: 草图唯一标识

        Returns:
            高清图 bytes

        Raises:
            DraftWorkflowError: 草稿不存在或尚未确认（无 result.bin）
        """
        draft = await self._load_draft(draft_id)
        if draft is None:
            raise DraftWorkflowError(f"Draft not found or expired: {draft_id}")
        draft_dir = self._draft_dir(draft.session_id, draft_id)
        result = self._read_result_bytes(draft_dir)
        if result is None:
            raise DraftWorkflowError(
                f"Draft result not available (not confirmed yet): {draft_id}"
            )
        return result

    async def delete_draft(self, draft_id: str) -> None:
        """删除单个草图: rmtree 文件目录 + 删 Redis 元数据 key.

        Args:
            draft_id: 草图唯一标识
        """
        import shutil

        # 先读 meta 拿 session_id 定位目录（Redis key 可能已过期但文件还在）
        draft = await self._load_draft(draft_id)
        session_id = draft.session_id if draft else None
        draft_dir = self._draft_dir(session_id, draft_id)

        shutil.rmtree(draft_dir, ignore_errors=True)

        key = self._make_redis_key(draft_id)
        if self._redis_client is not None:
            await self._redis_client.delete(key)
        else:
            self._memory_store.pop(key, None)

        if session_id:
            skey = self._make_session_index_key(session_id)
            if self._redis_client is not None:
                try:
                    await self._redis_client.srem(skey, draft_id)
                except Exception as exc:
                    logger.debug("session index srem failed: %s", exc)
            else:
                self._memory_session_index.get(session_id, set()).discard(draft_id)

        logger.debug(
            "generation_optimization.draft_generator.draft_deleted",
            extra={"draft_id": draft_id},
        )

    async def _delete_draft(self, draft_id: str) -> None:
        """从 Redis 删除草图元数据（保留文件，供 delete_session 统一 rmtree）.

        旧接口，保留向后兼容。新代码用 delete_draft（文件+Redis 一起删）。
        """
        key = self._make_redis_key(draft_id)
        if self._redis_client is not None:
            await self._redis_client.delete(key)
        else:
            self._memory_store.pop(key, None)

    async def delete_session(
        self,
        session_id: str,
        *,
        user_id: str | None = None,
        group_id: str | None = None,
    ) -> int:
        """删除一个会话的所有草稿 (DELETE /admin/drafts/session/{id} 用).

        rmtree {store_dir}/{session_id}/ 整个目录 + 删该 session 所有 Redis draft key。

        Args:
            session_id: 聊天会话 ID

        Returns:
            删除的草稿数量
        """
        safe_session_id = self._safe_path_component(session_id, "session_id")

        # 1) 收集该 session 下所有 draft_id（从 Redis set 或目录扫描）
        draft_ids: list[str] = []
        if self._redis_client is not None:
            skey = self._make_session_index_key(safe_session_id)
            try:
                members = await self._redis_client.smembers(skey)
                draft_ids = [m.decode() if isinstance(m, bytes) else m for m in members]
            except Exception as exc:
                logger.debug("session index smembers failed: %s", exc)
        else:
            draft_ids = list(self._memory_session_index.get(safe_session_id, set()))

        session_dir = os.path.join(self._store_dir, safe_session_id)
        session_has_entries = False
        if os.path.isdir(session_dir):
            for entry in os.listdir(session_dir):
                session_has_entries = True
                entry_path = os.path.join(session_dir, entry)
                if os.path.isdir(entry_path) and entry not in draft_ids:
                    if _SAFE_PATH_COMPONENT.fullmatch(entry) is None:
                        logger.warning(
                            "skipping invalid draft directory name during session deletion",
                            extra={"session_id": safe_session_id},
                        )
                        continue
                    draft_ids.append(entry)
        for draft_id in draft_ids:
            self._safe_path_component(draft_id, "draft_id")

        # API callers provide an owner identity. Verify every persisted draft
        # before deleting any key or directory so a guessed session ID cannot
        # delete another browser operator's media.
        if user_id is not None or group_id is not None:
            verified_metadata = False
            for draft_id in draft_ids:
                draft = await self._load_draft(draft_id)
                if draft is not None:
                    if draft.session_id != safe_session_id:
                        raise DraftWorkflowError("draft_session_forbidden")
                    draft_user_id = str(draft.user_id or "")
                    draft_group_id = str(draft.group_id or "")
                else:
                    meta = self._read_meta(os.path.join(session_dir, draft_id))
                    if meta is None:
                        continue
                    draft_user_id = str(meta.get("user_id") or "")
                    draft_group_id = str(meta.get("group_id") or "")
                verified_metadata = True
                if not draft_user_id and not draft_group_id:
                    raise DraftWorkflowError("draft_session_owner_unknown")
                if (
                    (draft_user_id and draft_user_id != str(user_id or ""))
                    or (draft_group_id and draft_group_id != str(group_id or ""))
                ):
                    raise DraftWorkflowError("draft_session_forbidden")
            if os.path.isdir(session_dir) and session_has_entries and not verified_metadata:
                raise DraftWorkflowError("draft_session_owner_unknown")

        # 2) 删每个 draft 的 Redis key
        deleted = 0
        for draft_id in draft_ids:
            key = self._make_redis_key(draft_id)
            if self._redis_client is not None:
                await self._redis_client.delete(key)
            else:
                self._memory_store.pop(key, None)
            deleted += 1

        # 3) 删 session 索引 set
        if self._redis_client is not None:
            try:
                await self._redis_client.delete(
                    self._make_session_index_key(safe_session_id)
                )
            except Exception as exc:
                logger.debug("session index delete failed: %s", exc)
        else:
            self._memory_session_index.pop(safe_session_id, None)

        # 4) rmtree 整个 session 目录（覆盖 Redis 已过期但文件残留的情况）
        if os.path.isdir(session_dir):
            shutil.rmtree(session_dir, ignore_errors=True)

        logger.info(
            "generation_optimization.draft_generator.session_deleted",
            extra={"session_id": safe_session_id, "deleted_count": deleted},
        )
        return deleted
