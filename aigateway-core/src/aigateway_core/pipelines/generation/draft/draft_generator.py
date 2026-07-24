"""
Draft Generator Strategy — 渐进式生成工作流核心逻辑
===================================================

管理 Draft-to-HiRes 工作流：
1. 生成低分辨率草图（图片默认 1024x1024 / 视频关键帧）
2. 确认后触发 Upscaler 放大到目标分辨率
3. 拒绝后重新生成（不缓存被拒绝的草图，立即释放资源）
4. 重试次数限制，耗尽后返回错误并保留最近草图
5. draft_id 唯一标识，24 小时过期自动释放
6. ComfyUI API 集成：当 ComfyUI 服务可用时使用真实生成，否则回退到占位实现

需求: 3.1, 3.2, 3.3, 3.4, 3.5, 3.7, 3.8, 3.9, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from aigateway_core.pipelines.generation._common.config import DraftWorkflowConfig
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation._common.models import (
    DRAFT_STATUS_CONFIRMED,
    DRAFT_STATUS_GENERATING,
    DRAFT_STATUS_PENDING,
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

# Default negative prompt for image generation
_DEFAULT_NEGATIVE_PROMPT = "ugly, blurry, low quality, distorted, deformed"


@dataclass
class _SuperResolveResult:
    """像素级超分辨率结果（内部类型）.

    携带放大后的图片字节和真实输出尺寸（等比放大后可能与请求的
    target_resolution 不同），供调用方校正 UpscaleResult.target_resolution。
    """

    output_bytes: bytes
    output_resolution: Optional[Tuple[int, int]] = None


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

    当 ComfyUI 服务可用时，使用 ComfyUI API 执行真实图像/视频生成。
    当 ComfyUI 不可用或执行失败时，回退到占位实现。

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
        comfyui_config: Optional[ComfyUIConfig] = None,
        store_dir: Optional[str] = None,
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
        # litellm_bridge 延迟绑定（由 main.py 注入），用于低分辨率预览生成
        self._litellm_bridge: Any = None
        # 超分模型懒加载锁：避免并发 confirm 同时构造 RealESRGANer（~64MB 权重重复加载）
        self._sr_model: Any = None
        self._sr_model_lock = asyncio.Lock()
        # 超分推理串行锁：PyTorch module.forward() 非线程安全，并发 enhance 会竞态
        # 共享张量导致输出损坏或崩溃。enhance 在线程池执行，故用 threading.Lock
        # （而非 asyncio.Lock）保护线程内的临界区。
        self._sr_infer_lock = threading.Lock()
        # In-memory fallback when no Redis client is provided (for testing)
        self._memory_store: Dict[str, str] = {}
        # session → set(draft_id) 的内存镜像（无 Redis 时测试用）
        self._memory_session_index: Dict[str, set] = {}
        # 后台生成任务强引用集合。asyncio.create_task 返回的 Task 仅被事件循环的
        # WeakSet 持有，CPython GC 可能在 submit_draft 返回后回收未完成的协程，
        # 导致 Redis 状态永久卡在 generating、前端轮询到超时。这里持有强引用，
        # 任务完成后通过 add_done_callback 自动移除。
        self._bg_tasks: set = set()

    async def generate_draft(
        self,
        request: GenerationRequest,
        config: DraftWorkflowConfig,
        keyframe_count: Optional[int] = None,
        chat_session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        group_id: Optional[str] = None,
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
        keyframe_count: Optional[int] = None,
        chat_session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        group_id: Optional[str] = None,
    ) -> DraftResult:
        """提交草稿生成任务（异步）— 立即返回 draft_id (status=generating)."""
        draft_id = uuid.uuid4().hex
        now = time.time()
        ttl_seconds = config.retention_period_hours * 3600
        expires_at = now + ttl_seconds

        is_video = self._is_video_request(request)
        media_type = "video" if is_video else "image"

        # generation_params 快照（后台 task 也要用，这里先建好）
        generation_params: Dict[str, Any] = {
            "prompt": request.prompt,
            "target_resolution": list(request.target_resolution),
            "media_type": media_type,
            "draft_resolution": list(config.draft_resolution),
            "request_id": request.request_id,
        }
        if is_video and keyframe_count is not None:
            generation_params["explicit_keyframe_count"] = keyframe_count

        # 占位 DraftResult：status=generating，previews 空
        draft = DraftResult(
            draft_id=draft_id,
            previews=[],
            generation_params=generation_params,
            created_at=now,
            expires_at=expires_at,
            attempt_number=1,
            max_attempts=config.max_regeneration_attempts,
            status=DRAFT_STATUS_GENERATING,
            media_type=media_type,
            session_id=chat_session_id,
            user_id=user_id,
            group_id=group_id,
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
                    },
                    ttl_seconds=ttl_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("TaskTracker register draft failed: %s", exc)

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

    async def _generate_draft_async(
        self,
        draft_id: str,
        request: GenerationRequest,
        config: DraftWorkflowConfig,
        keyframe_count: Optional[int],
        is_video: bool,
        media_type: str,
        generation_params: Dict[str, Any],
        ttl_seconds: int,
        expires_at: float,
        chat_session_id: Optional[str],
        user_id: Optional[str],
        group_id: Optional[str],
    ) -> None:
        """后台生成预览（由 submit_draft 用 asyncio.create_task 起动）.

        完成后：写 preview.bin + 更新 meta/Redis status=pending + TaskTracker succeeded。
        失败：status=failed + TaskTracker failed，错误写 meta。
        """
        start_time = time.monotonic()
        try:
            # 探测 ComfyUI（每次生成前探一次，可用则真生成，否则占位降级）
            try:
                await self._check_comfyui()
            except Exception as exc:
                logger.warning("ComfyUI 探测失败，走占位降级: %s", exc)

            if is_video:
                num_keyframes = self._calculate_keyframe_count(
                    request, config, keyframe_count
                )
                previews = await self._generate_video_previews_with_comfyui(
                    request, config, num_keyframes
                )
            else:
                previews = [await self._generate_image_preview_with_comfyui(request, config)]

            draft = DraftResult(
                draft_id=draft_id,
                previews=previews,
                generation_params=generation_params,
                created_at=time.time(),
                expires_at=expires_at,
                attempt_number=1,
                max_attempts=config.max_regeneration_attempts,
                status=DRAFT_STATUS_PENDING,
                media_type=media_type,
                session_id=chat_session_id,
                user_id=user_id,
                group_id=group_id,
            )
            await self._store_draft(draft, max(1, int(expires_at - time.time())))

            if self._task_tracker is not None:
                try:
                    await self._task_tracker.update_status(
                        "draft", draft_id, "succeeded",
                        metadata={"preview_count": len(previews)},
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("TaskTracker update succeeded failed: %s", exc)

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
            # 标记 failed（写 meta + Redis，前端轮询据此报错）
            try:
                draft_dir = self._ensure_draft_dir(chat_session_id, draft_id)
                meta = self._read_meta(draft_dir) or {}
                meta.update({
                    "draft_id": draft_id,
                    "session_id": chat_session_id,
                    "user_id": user_id,
                    "group_id": group_id,
                    "media_type": media_type,
                    "status": "failed",
                    "expires_at": expires_at,
                    "error": str(exc),
                })
                tmp = self._meta_path(draft_dir) + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(meta, f)
                os.replace(tmp, self._meta_path(draft_dir))

                # 更新 Redis 元数据 status=failed
                key = self._make_redis_key(draft_id)
                if self._redis_client is not None:
                    raw = await self._redis_client.get(key)
                else:
                    raw = self._memory_store.get(key)
                if raw is not None:
                    raw = raw.decode() if isinstance(raw, bytes) else raw
                    data = json.loads(raw)
                    data["status"] = "failed"
                    ttl_remaining = max(1, int(expires_at - time.time()))
                    if self._redis_client is not None:
                        await self._redis_client.set(key, json.dumps(data), ex=ttl_remaining)
                    else:
                        self._memory_store[key] = json.dumps(data)
            except Exception:  # noqa: BLE001
                logger.error("failed to mark draft %s as failed", draft_id, exc_info=True)

            if self._task_tracker is not None:
                try:
                    await self._task_tracker.update_status(
                        "draft", draft_id, "failed", metadata={"error": str(exc)}
                    )
                except Exception as exc2:  # noqa: BLE001
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
        draft = await self._load_draft(draft_id)
        if draft is None:
            raise DraftWorkflowError(
                f"Draft not found or expired: {draft_id}"
            )

        if draft.status != DRAFT_STATUS_PENDING:
            raise DraftWorkflowError(
                f"Draft cannot be confirmed: status is '{draft.status}', "
                f"expected 'pending'. draft_id={draft_id}"
            )

        # Check if draft has expired
        if time.time() > draft.expires_at:
            raise DraftWorkflowError(
                f"Draft has expired: {draft_id}"
            )

        # Update status to confirmed
        draft.status = DRAFT_STATUS_CONFIRMED
        ttl_remaining = max(1, int(draft.expires_at - time.time()))
        await self._store_draft(draft, ttl_remaining)

        # Upscale to target resolution via pixel-level super-resolution
        target_resolution = self._get_target_resolution(draft)
        start_time = time.monotonic()

        # Try pixel-level super-resolution (RealESRGAN) first
        sr_result = await self._super_resolve(draft, target_resolution)
        if sr_result is not None:
            output_data = sr_result.output_bytes
            # 超分按等比放大到长边 4096，实际输出尺寸可能与 target_resolution 不同，
            # 用真实输出尺寸覆盖，避免 UpscaleResult.target_resolution 撒谎。
            actual_resolution = sr_result.output_resolution or target_resolution
            algorithm_used = "real-esrgan"
        else:
            # Fallback to ComfyUI upscale
            comfyui_result = await self._upscale_with_comfyui(draft, target_resolution)
            if comfyui_result is not None:
                output_data = comfyui_result
                actual_resolution = target_resolution
                algorithm_used = "comfyui"
            else:
                output_data = self._simulate_upscale(draft, target_resolution)
                actual_resolution = target_resolution
                algorithm_used = self._config.upscale_algorithm

        duration_ms = (time.monotonic() - start_time) * 1000.0

        result = UpscaleResult(
            draft_id=draft_id,
            output_data=output_data,
            target_resolution=actual_resolution,
            algorithm_used=algorithm_used,
            duration_ms=duration_ms,
        )

        # 持久化高清结果到文件（修复原"confirm 后 output_data 仅内存返回、未落盘"bug）。
        # GET /admin/draft/{id}/result 通过 get_result_bytes 读取此文件，刷新后可重取。
        try:
            draft_dir = self._ensure_draft_dir(draft.session_id, draft_id)
            self._write_result_bytes(draft_dir, output_data)
        except Exception as exc:  # noqa: BLE001
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

        # Generate new draft with incremented attempt number
        new_draft = await self._regenerate_draft(draft)

        return new_draft

    async def get_draft(self, draft_id: str) -> Optional[DraftResult]:
        """获取草图信息.

        Args:
            draft_id: 草图唯一标识

        Returns:
            DraftResult 或 None（不存在/已过期）
        """
        return await self._load_draft(draft_id)

    # ===================================================================
    # ComfyUI API 集成方法
    # ===================================================================

    async def _check_comfyui(self) -> None:
        """检测 ComfyUI 服务是否可达.

        通过 GET /system_stats 端点检测连接。
        设置 self._comfyui_available 标志。
        """
        try:
            import httpx
        except ImportError:
            logger.warning(
                "httpx 未安装，ComfyUI 集成不可用，回退到占位实现"
            )
            self._comfyui_available = False
            return

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
                    logger.warning(
                        "ComfyUI 服务返回非 200 状态: %d，回退到占位实现",
                        response.status_code,
                    )
        except Exception as exc:
            self._comfyui_available = False
            logger.warning(
                "ComfyUI 服务不可达: %s，回退到占位实现", exc
            )

    async def _submit_workflow(self, workflow_json: dict) -> str:
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
        payload = {"prompt": workflow_json}

        async with httpx.AsyncClient(
            timeout=self._comfyui_config.connect_timeout
        ) as client:
            response = await client.post(url, json=payload)
            if response.status_code != 200:
                raise DraftWorkflowError(
                    f"ComfyUI workflow submission failed: "
                    f"status={response.status_code}, body={response.text}"
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

    async def _poll_result(
        self, prompt_id: str, timeout: Optional[int] = None
    ) -> bytes:
        """轮询 ComfyUI 获取工作流执行结果.

        通过 GET /history/{prompt_id} 轮询直到工作流完成，
        然后获取输出图片数据。

        Args:
            prompt_id: 工作流提交返回的 prompt_id
            timeout: 超时时间/秒，默认使用 comfyui_config.execution_timeout

        Returns:
            输出图片/帧的 bytes 数据

        Raises:
            DraftWorkflowError: 轮询超时或获取结果失败
        """
        import httpx

        if timeout is None:
            timeout = self._comfyui_config.execution_timeout

        history_url = f"{self._comfyui_config.server_url}/history/{prompt_id}"
        poll_interval = 1.0  # seconds
        elapsed = 0.0

        async with httpx.AsyncClient(
            timeout=self._comfyui_config.connect_timeout
        ) as client:
            while elapsed < timeout:
                response = await client.get(history_url)
                if response.status_code == 200:
                    history = response.json()
                    if prompt_id in history:
                        # Workflow completed — extract output image
                        prompt_data = history[prompt_id]
                        outputs = prompt_data.get("outputs", {})
                        # Find the first node with images output
                        for _node_id, node_output in outputs.items():
                            images = node_output.get("images", [])
                            if images:
                                # Fetch the first image
                                image_info = images[0]
                                filename = image_info.get("filename", "")
                                subfolder = image_info.get("subfolder", "")
                                img_type = image_info.get("type", "output")
                                view_url = (
                                    f"{self._comfyui_config.server_url}/view"
                                    f"?filename={filename}"
                                    f"&subfolder={subfolder}"
                                    f"&type={img_type}"
                                )
                                img_response = await client.get(view_url)
                                if img_response.status_code == 200:
                                    logger.info(
                                        "generation_optimization.draft_generator.result_received",
                                        extra={
                                            "prompt_id": prompt_id,
                                            "filename": filename,
                                        },
                                    )
                                    return img_response.content
                        # No images found in outputs
                        raise DraftWorkflowError(
                            f"ComfyUI 工作流完成但无图片输出: prompt_id={prompt_id}"
                        )

                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

        raise DraftWorkflowError(
            f"ComfyUI 工作流执行超时 ({timeout}s): prompt_id={prompt_id}"
        )

    # ===================================================================
    # ComfyUI 工作流 JSON 构建器
    # ===================================================================

    def _build_image_draft_workflow(self, request: GenerationRequest, config: Optional[DraftWorkflowConfig] = None) -> dict:
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
        prompt_text = request.prompt or "a beautiful image"
        draft_w, draft_h = cfg.draft_resolution

        workflow: dict = {
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": int(uuid.uuid4().int % (2**32)),
                    "steps": 20,
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
                    "ckpt_name": "sd_xl_base_1.0.safetensors",
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

    def _build_upscale_workflow(
        self, draft_data: bytes, target_resolution: Tuple[int, int]
    ) -> dict:
        """构建 Real-ESRGAN/SUPIR 放大工作流 JSON.

        工作流包含:
        - LoadImage: 加载草图图片
        - UpscaleModelLoader: 加载放大模型 (RealESRGAN_x4plus)
        - ImageUpscaleWithModel: 执行放大
        - SaveImage: 保存输出

        对于大于 4x 放大倍率的目标分辨率，使用 SUPIR 模型。

        Args:
            draft_data: 草图图片 bytes 数据
            target_resolution: 目标分辨率 (width, height)

        Returns:
            ComfyUI 标准格式工作流 JSON dict

        需求: 4.3
        """
        target_width, target_height = target_resolution

        # Choose upscale model based on scale factor
        # 草稿分辨率由 config.draft_resolution 决定（默认 1024×1024）
        draft_w, draft_h = self._config.draft_resolution
        scale_factor = max(target_width / draft_w, target_height / draft_h)
        if scale_factor > 4.0:
            upscale_model = "SUPIR"
        else:
            upscale_model = "RealESRGAN_x4plus"

        # Encode draft_data as base64 for LoadImage node reference
        draft_b64 = base64.b64encode(draft_data).decode("ascii")

        workflow: dict = {
            "1": {
                "class_type": "LoadImage",
                "inputs": {
                    "image": draft_b64,
                    "upload": "image",
                },
            },
            "2": {
                "class_type": "UpscaleModelLoader",
                "inputs": {
                    "model_name": upscale_model,
                },
            },
            "3": {
                "class_type": "ImageUpscaleWithModel",
                "inputs": {
                    "upscale_model": ["2", 0],
                    "image": ["1", 0],
                },
            },
            "4": {
                "class_type": "SaveImage",
                "inputs": {
                    "filename_prefix": "upscaled",
                    "images": ["3", 0],
                },
            },
        }

        return workflow

    def _build_video_draft_workflow(self, request: GenerationRequest) -> dict:
        """构建 AnimateDiff/LTX-Video 关键帧生成工作流 JSON.

        工作流包含:
        - CheckpointLoaderSimple: 加载 base 模型
        - AnimateDiffLoaderWithContext: 加载 AnimateDiff 运动模块
        - CLIPTextEncode (positive/negative): 编码 prompt
        - EmptyLatentImage: 512x512 潜空间 (batch = keyframe_count)
        - KSampler: 使用 AnimateDiff 上下文采样
        - VAEDecode: 解码为图片序列
        - SaveImage: 保存关键帧

        Args:
            request: 生成请求

        Returns:
            ComfyUI 标准格式工作流 JSON dict

        需求: 4.4
        """
        prompt_text = request.prompt or "a beautiful animation"
        # Calculate keyframe count based on config
        num_keyframes = max(
            2,
            math.ceil(
                self._config.preview_video_duration_seconds
                / self._config.preview_keyframe_interval_seconds
            ),
        )

        workflow: dict = {
            "1": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {
                    "ckpt_name": "sd_xl_base_1.0.safetensors",
                },
            },
            "2": {
                "class_type": "AnimateDiffLoaderWithContext",
                "inputs": {
                    "model": ["1", 0],
                    "motion_module": "AnimateDiff_v2.ckpt",
                    "context_length": num_keyframes,
                    "context_overlap": 4,
                },
            },
            "3": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "text": prompt_text,
                    "clip": ["1", 1],
                },
            },
            "4": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "text": _DEFAULT_NEGATIVE_PROMPT,
                    "clip": ["1", 1],
                },
            },
            "5": {
                "class_type": "EmptyLatentImage",
                "inputs": {
                    "width": 512,
                    "height": 512,
                    "batch_size": num_keyframes,
                },
            },
            "6": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": int(uuid.uuid4().int % (2**32)),
                    "steps": 20,
                    "cfg": 7.5,
                    "sampler_name": "euler",
                    "scheduler": "normal",
                    "denoise": 1.0,
                    "model": ["2", 0],
                    "positive": ["3", 0],
                    "negative": ["4", 0],
                    "latent_image": ["5", 0],
                },
            },
            "7": {
                "class_type": "VAEDecode",
                "inputs": {
                    "samples": ["6", 0],
                    "vae": ["1", 2],
                },
            },
            "8": {
                "class_type": "SaveImage",
                "inputs": {
                    "filename_prefix": f"video_draft_{request.request_id}",
                    "images": ["7", 0],
                },
            },
        }

        return workflow

    # ===================================================================
    # 内部方法 — ComfyUI 集成预览生成
    # ===================================================================

    async def _generate_image_preview_with_comfyui(
        self,
        request: GenerationRequest,
        config: DraftWorkflowConfig,
    ) -> bytes:
        """尝试通过 ComfyUI 生成图片预览，失败时回退到占位实现.

        Args:
            request: 生成请求
            config: Draft 工作流配置

        Returns:
            预览图 bytes 数据
        """
        if self._comfyui_available:
            try:
                workflow = self._build_image_draft_workflow(request, config)
                prompt_id = await self._submit_workflow(workflow)
                image_data = await self._poll_result(prompt_id)
                logger.info(
                    "generation_optimization.draft_generator.comfyui_image_preview",
                    extra={"request_id": request.request_id, "size": len(image_data)},
                )
                return image_data
            except Exception as exc:
                logger.warning(
                    "ComfyUI 图片预览生成失败，回退到占位实现: %s", exc
                )

        # Fallback: 优先用 Agnes(litellm_bridge)生成真实低分辨率预览，否则占位
        return await self._generate_image_preview(request, config)

    async def _generate_video_previews_with_comfyui(
        self,
        request: GenerationRequest,
        config: DraftWorkflowConfig,
        num_keyframes: int,
    ) -> list[bytes]:
        """尝试通过 ComfyUI 生成视频关键帧，失败时回退到占位实现.

        Args:
            request: 生成请求
            config: Draft 工作流配置
            num_keyframes: 关键帧数量

        Returns:
            关键帧 bytes 列表
        """
        if self._comfyui_available:
            try:
                workflow = self._build_video_draft_workflow(request)
                prompt_id = await self._submit_workflow(workflow)
                # For video, the output may be multiple frames
                # Poll for the result and use it as single combined output
                video_data = await self._poll_result(prompt_id)
                # Split into keyframes (or use the single output for each frame)
                # In practice ComfyUI returns batch images; here we return
                # the same data for each keyframe as a simplified approach
                previews = [video_data] * num_keyframes
                logger.info(
                    "generation_optimization.draft_generator.comfyui_video_preview",
                    extra={
                        "request_id": request.request_id,
                        "num_keyframes": num_keyframes,
                    },
                )
                return previews
            except Exception as exc:
                logger.warning(
                    "ComfyUI 视频关键帧生成失败，回退到占位实现: %s", exc
                )

        # Fallback to placeholder
        return self._generate_keyframe_previews(request, config, num_keyframes)

    async def _upscale_with_comfyui(
        self,
        draft: DraftResult,
        target_resolution: Tuple[int, int],
    ) -> Optional[bytes]:
        """尝试通过 ComfyUI 执行高清放大.

        Args:
            draft: 草图结果
            target_resolution: 目标分辨率

        Returns:
            放大后 bytes 数据，失败返回 None
        """
        if not self._comfyui_available:
            return None

        try:
            # Use the first preview as input for upscale
            draft_data = draft.previews[0] if draft.previews else b""
            if not draft_data:
                return None

            workflow = self._build_upscale_workflow(draft_data, target_resolution)
            prompt_id = await self._submit_workflow(workflow)
            result_data = await self._poll_result(prompt_id)
            logger.info(
                "generation_optimization.draft_generator.comfyui_upscale",
                extra={
                    "draft_id": draft.draft_id,
                    "target_resolution": target_resolution,
                },
            )
            return result_data
        except Exception as exc:
            logger.warning(
                "ComfyUI 放大失败，回退到占位实现: %s", exc
            )
            return None

    # ===================================================================
    # 内部方法 — 像素级超分辨率
    # ===================================================================

    def _build_sr_model(self, model_path: str) -> Any:
        """同步构造 RealESRGANer（含 ~64MB 权重加载）。

        预期由 run_in_executor 调用，不在事件循环线程执行。返回构造好的
        RealESRGANer，或权重/依赖缺失时返回 None。
        """
        try:
            import torch
            from realesrgan import RealESRGANer
            from basicsr.archs.rrdbnet_arch import RRDBNet
        except ImportError as exc:
            logger.warning("RealESRGAN 依赖未就绪，跳过超分: %s", exc)
            return None
        # RealESRGAN_x4plus 对应 RRDBNet(x4) 架构
        device = torch.device('cuda') if torch.cuda.is_available() else 'cpu'
        model = RRDBNet(
            num_in_ch=3,
            num_out_ch=3,
            num_feat=64,
            num_block=23,
            num_grow_ch=32,
            scale=4,
        )
        return RealESRGANer(
            scale=4,  # RealESRGAN 默认 4x 放大
            model_path=model_path,
            model=model,
            half=torch.cuda.is_available(),  # FP16 加速(仅 GPU)
            device=device,
            tile=512,  # 分块推理, 降低显存峰值
            tile_pad=10,
        )

    async def _super_resolve(
        self,
        draft: DraftResult,
        target_resolution: Tuple[int, int],
    ) -> Optional[_SuperResolveResult]:
        """像素级超分辨率放大.

        使用 RealESRGAN (RRDBNet x4) 将草稿图片等比放大到长边 4096，
        不调用 LLM API，只消耗本地计算资源。等比放大保持长宽比、不裁剪。

        Args:
            draft: 已确认的草图结果
            target_resolution: 请求的目标分辨率（仅用于日志/回退参考；
                超分实际按等比放大到长边 4096，输出尺寸由真实源图决定）

        Returns:
            _SuperResolveResult（含输出字节和真实输出尺寸），失败返回 None
        """
        if not draft.previews:
            return None

        draft_data = draft.previews[0]

        try:
            import cv2
            import numpy as np
            import torch
            from realesrgan import RealESRGANer
            from basicsr.archs.rrdbnet_arch import RRDBNet
        except ImportError as exc:
            # realesrgan/basicsr/cv2/torch 任一缺失（或 basicsr 的 functional_tensor
            # 兼容补丁未生效）都会在此暴露。warning 而非 debug，避免超分静默失效。
            logger.warning("RealESRGAN 依赖未就绪，跳过超分: %s", exc)
            return None

        # 先解码图片拿到真实尺寸（Agnes 可能不严格遵循请求的 size）
        try:
            img_array = cv2.imdecode(
                np.frombuffer(draft_data, np.uint8),
                cv2.IMREAD_COLOR,
            )
        except Exception as exc:
            logger.warning("草稿图片解码失败，跳过超分: %s", exc)
            return None
        if img_array is None:
            logger.warning("草稿图片解码为空（数据非有效图片），跳过超分")
            return None

        src_h, src_w = img_array.shape[0], img_array.shape[1]
        # 等比放大到长边 4096（保持长宽比，不裁剪）
        # outscale 是相对原始输入的总缩放因子（RealESRGANer.enhance 内部
        # cv2.resize 到 w_input*outscale），故 scale_factor = 4096 / 源长边。
        sr_target = 4096
        scale_factor = sr_target / max(src_w, src_h)

        try:
            # 权重路径: Dockerfile 预下载到 /app/weights/RealESRGAN_x4plus.pth
            # 本地开发环境回退到包内置 weights 目录或环境变量指定路径
            model_path = os.environ.get(
                "RealESRGAN_MODEL_PATH",
                "/app/weights/RealESRGAN_x4plus.pth",
            )
            if not os.path.isfile(model_path):
                try:
                    from realesrgan import ROOT_DIR
                    model_path = os.path.join(ROOT_DIR, "weights", "RealESRGAN_x4plus.pth")
                except Exception:
                    pass
            if not os.path.isfile(model_path):
                logger.warning("RealESRGAN 权重未找到，跳过超分: %s", model_path)
                return None

            # 加锁懒加载超分模型，避免并发 confirm 重复构造 RealESRGANer
            if self._sr_model is None:
                async with self._sr_model_lock:
                    if self._sr_model is None:
                        # 模型构造 + ~64MB 权重加载是同步重 I/O/CPU，
                        # 卸载到线程池避免阻塞单 worker uvicorn 事件循环。
                        loop = asyncio.get_running_loop()
                        built = await loop.run_in_executor(
                            None, lambda: self._build_sr_model(model_path)
                        )
                        if built is None:
                            return None
                        self._sr_model = built

            # enhance 是同步重计算（CPU 上 1024→4096 数秒到数十秒），
            # 卸载到线程池避免阻塞单 worker uvicorn 事件循环。
            # PyTorch module.forward() 非线程安全：并发 enhance 会竞态共享张量，
            # 故用 _sr_infer_lock 串行化推理（在线程内加锁，保护真正的临界区）。
            def _do_enhance():
                with self._sr_infer_lock:
                    return self._sr_model.enhance(img_array, outscale=scale_factor)

            loop = asyncio.get_running_loop()
            output, _ = await loop.run_in_executor(None, _do_enhance)

            _, encoded = cv2.imencode('.png', output)
            out_h, out_w = output.shape[0], output.shape[1]
            return _SuperResolveResult(
                output_bytes=encoded.tobytes(),
                output_resolution=(out_w, out_h),
            )

        except Exception as exc:
            logger.warning("超分失败: %s", exc)
            return None

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
        explicit_count: Optional[int] = None,
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
        ).encode("utf-8")
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

        from urllib.parse import urlparse
        import socket

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
                ip_int = socket.inet_aton(ip)
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
                if ip == "::1" or ip.startswith("fe80:") or ip.startswith("fc") or ip.startswith("fd"):
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
                                if redirect_ip.startswith("127.") or redirect_ip == "0.0.0.0" or redirect_ip.startswith("10.") or redirect_ip.startswith("169.254.") or redirect_ip.startswith("192.168.") or redirect_ip.startswith("::1") or redirect_ip.startswith("fe80:") or redirect_ip.startswith("fc") or redirect_ip.startswith("fd"):
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
            ).encode("utf-8")
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
        ).encode("utf-8")
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

        # Determine media type from old draft's generation_params
        media_type = old_draft.generation_params.get("media_type", "image")
        is_video = media_type == "video"

        # Regenerate previews
        width, height = self._config.draft_resolution
        if is_video:
            num_previews = len(old_draft.previews)
            previews: list[bytes] = []
            for i in range(num_previews):
                placeholder = (
                    f"DRAFT_PREVIEW:video_keyframe:{width}x{height}:"
                    f"frame={i}/{num_previews}:"
                    f"regenerated:attempt={old_draft.attempt_number + 1}:"
                    f"id={new_draft_id}"
                ).encode("utf-8")
                previews.append(placeholder)
        else:
            placeholder = (
                f"DRAFT_PREVIEW:image:{width}x{height}:"
                f"regenerated:attempt={old_draft.attempt_number + 1}:"
                f"id={new_draft_id}"
            ).encode("utf-8")
            previews = [placeholder]

        new_draft = DraftResult(
            draft_id=new_draft_id,
            previews=previews,
            generation_params=old_draft.generation_params.copy(),
            created_at=now,
            expires_at=expires_at,
            attempt_number=old_draft.attempt_number + 1,
            max_attempts=old_draft.max_attempts,
            status=DRAFT_STATUS_PENDING,
            media_type=media_type,
            session_id=old_draft.session_id,
            user_id=old_draft.user_id,
            group_id=old_draft.group_id,
        )

        # Store new draft
        await self._store_draft(new_draft, ttl_seconds)

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
        return f"{_DRAFT_SESSION_KEY_PREFIX}:{session_id}"

    def _draft_dir(self, session_id: Optional[str], draft_id: str) -> str:
        """草稿文件目录路径: {store_dir}/{session_id or 'unknown'}/{draft_id}/.

        仅计算路径，**不创建目录**。session_id 缺失时归入 'unknown' 桶。
        读路径（_load_draft / get_result_bytes / delete_draft）调用本函数——
        若此处 makedirs，会对 Redis key 已过期但文件已被 cleaner 删除的草稿
        重建空目录，cleaner 的 mtime 兜底要到 24h 后才回收，造成磁盘泄漏。
        需要确保目录存在的写路径请改用 _ensure_draft_dir。
        """
        sid = session_id or "unknown"
        return os.path.join(self._store_dir, sid, draft_id)

    def _ensure_draft_dir(self, session_id: Optional[str], draft_id: str) -> str:
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
        }
        tmp = self._meta_path(draft_dir) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        os.replace(tmp, self._meta_path(draft_dir))  # 原子写，防 cleaner 读半截

    def _read_meta(self, draft_dir: str) -> Optional[Dict[str, Any]]:
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
        self, draft_dir: str, previews: List[bytes], media_type: str
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
    ) -> List[bytes]:
        """读预览 bytes. 文件缺失返回空列表."""
        result: List[bytes] = []
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

    def _read_result_bytes(self, draft_dir: str) -> Optional[bytes]:
        """读高清结果 bytes；不存在返回 None."""
        p = os.path.join(draft_dir, "result.bin")
        if not os.path.isfile(p):
            return None
        with open(p, "rb") as f:
            return f.read()

    async def _index_draft_to_session(
        self, session_id: Optional[str], draft_id: str, ttl_seconds: int
    ) -> None:
        """把 draft_id 加入 session 索引集合（delete_session 批量删 key 用）."""
        if not session_id:
            return
        key = self._make_session_index_key(session_id)
        if self._redis_client is not None:
            try:
                await self._redis_client.sadd(key, draft_id)
                await self._redis_client.expire(key, ttl_seconds)
            except Exception as exc:  # noqa: BLE001
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
            "store_dir": draft_dir,  # 供 _load_draft 定位文件
        }
        data = json.dumps(serialized)

        if self._redis_client is not None:
            await self._redis_client.set(key := self._make_redis_key(draft.draft_id), data, ex=ttl_seconds)
        else:
            self._memory_store[self._make_redis_key(draft.draft_id)] = data

        await self._index_draft_to_session(draft.session_id, draft.draft_id, ttl_seconds)

    async def _load_draft(self, draft_id: str) -> Optional[DraftResult]:
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

        media_type = data.get("media_type", "image")
        draft_dir = data.get("store_dir") or self._draft_dir(data.get("session_id"), draft_id)

        # previews 懒加载：generating 阶段无文件，返回空列表（调用方按需再读）
        previews = self._read_preview_bytes(draft_dir, media_type) if data.get("status") != DRAFT_STATUS_GENERATING else []

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
                except Exception as exc:  # noqa: BLE001
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

    async def delete_session(self, session_id: str) -> int:
        """删除一个会话的所有草稿 (DELETE /admin/drafts/session/{id} 用).

        rmtree {store_dir}/{session_id}/ 整个目录 + 删该 session 所有 Redis draft key。

        Args:
            session_id: 聊天会话 ID

        Returns:
            删除的草稿数量
        """
        import shutil

        # 1) 收集该 session 下所有 draft_id（从 Redis set 或目录扫描）
        draft_ids: list[str] = []
        if self._redis_client is not None:
            skey = self._make_session_index_key(session_id)
            try:
                members = await self._redis_client.smembers(skey)
                draft_ids = [m.decode() if isinstance(m, bytes) else m for m in members]
            except Exception as exc:  # noqa: BLE001
                logger.debug("session index smembers failed: %s", exc)
        else:
            draft_ids = list(self._memory_session_index.get(session_id, set()))

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
                await self._redis_client.delete(self._make_session_index_key(session_id))
            except Exception as exc:  # noqa: BLE001
                logger.debug("session index delete failed: %s", exc)
        else:
            self._memory_session_index.pop(session_id, None)

        # 4) rmtree 整个 session 目录（覆盖 Redis 已过期但文件残留的情况）
        session_dir = os.path.join(self._store_dir, session_id)
        if os.path.isdir(session_dir):
            shutil.rmtree(session_dir, ignore_errors=True)

        logger.info(
            "generation_optimization.draft_generator.session_deleted",
            extra={"session_id": session_id, "deleted_count": deleted},
        )
        return deleted

