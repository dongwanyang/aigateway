"""
Draft Generator Strategy — 渐进式生成工作流核心逻辑
===================================================

管理 Draft-to-HiRes 工作流：
1. 生成低分辨率草图（图片 512x512 / 视频关键帧）
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
import time
import uuid
from typing import Any, Dict, Optional, Tuple

from aigateway_core.generation_optimization.config import DraftWorkflowConfig
from aigateway_core.generation_optimization.exceptions import DraftWorkflowError
from aigateway_core.generation_optimization.models import (
    DRAFT_STATUS_CONFIRMED,
    DRAFT_STATUS_PENDING,
    DraftResult,
    GenerationRequest,
    UpscaleResult,
)
from aigateway_core.shared.integration_configs import ComfyUIConfig

logger = logging.getLogger(__name__)

# Redis key prefix for draft storage
_DRAFT_KEY_PREFIX = "aigateway:draft"

# Default negative prompt for image generation
_DEFAULT_NEGATIVE_PROMPT = "ugly, blurry, low quality, distorted, deformed"


class DraftGeneratorStrategy:
    """草图生成器 — 管理 Draft-to-HiRes 工作流.

    负责生成低分辨率草图供用户预览确认，确认后执行高清放大，
    拒绝后重新生成。所有草图存储在 Redis 中，带 TTL 自动过期。

    当 ComfyUI 服务可用时，使用 ComfyUI API 执行真实图像/视频生成。
    当 ComfyUI 不可用或执行失败时，回退到占位实现。

    Attributes:
        _config: Draft 工作流配置
        _redis_client: Redis 客户端实例（需支持 async get/set/delete/expire）
        _comfyui_config: ComfyUI API 连接配置
        _comfyui_available: ComfyUI 服务是否可用
    """

    def __init__(
        self,
        config: DraftWorkflowConfig,
        redis_client: Any = None,
        comfyui_config: Optional[ComfyUIConfig] = None,
    ) -> None:
        """初始化 DraftGeneratorStrategy.

        Args:
            config: Draft-to-HiRes 工作流配置
            redis_client: Redis 客户端实例。若为 None，则使用内存字典模拟。
            comfyui_config: ComfyUI API 连接配置。若为 None，使用默认配置。
        """
        self._config = config
        self._redis_client = redis_client
        self._comfyui_config = comfyui_config or ComfyUIConfig()
        self._comfyui_available: bool = False
        # In-memory fallback when no Redis client is provided (for testing)
        self._memory_store: Dict[str, str] = {}

    async def generate_draft(
        self,
        request: GenerationRequest,
        config: DraftWorkflowConfig,
        keyframe_count: Optional[int] = None,
    ) -> DraftResult:
        """生成低分辨率草图/关键帧.

        图片请求: 生成 512x512 预览（单张）
        视频请求: 按时间间隔动态生成关键帧
            - 默认每 5 秒一帧，最少 2 帧（首末帧）
            - 用户可显式指定 keyframe_count 覆盖

        Args:
            request: 生成请求
            config: Draft 工作流配置（允许运行时覆盖）
            keyframe_count: 用户显式指定的关键帧数量，覆盖间隔计算

        Returns:
            DraftResult 包含 draft_id、previews、过期时间等
        """
        # 探测 ComfyUI 服务是否可用（原代码从未调用，_comfyui_available 恒为 False，
        # 导致永远走字符串占位）。这里在每次生成前探测一次，可用则走真生成，
        # 不可用则走占位降级（_generate_image_preview 等已实现）。
        try:
            await self._check_comfyui()
        except Exception as exc:
            logger.warning("ComfyUI 探测失败，走占位降级: %s", exc)

        draft_id = uuid.uuid4().hex
        now = time.time()
        ttl_seconds = config.retention_period_hours * 3600
        expires_at = now + ttl_seconds

        # Determine if this is a video request
        is_video = self._is_video_request(request)

        if is_video:
            # Video: generate keyframes
            num_keyframes = self._calculate_keyframe_count(
                request, config, keyframe_count
            )
            previews = await self._generate_video_previews_with_comfyui(
                request, config, num_keyframes
            )
        else:
            # Image: single 512x512 preview
            previews = [await self._generate_image_preview_with_comfyui(request, config)]

        # Build generation params snapshot
        generation_params: Dict[str, Any] = {
            "prompt": request.prompt,
            "target_resolution": list(request.target_resolution),
            "media_type": "video" if is_video else "image",
            "draft_resolution": list(config.draft_resolution),
            "request_id": request.request_id,
        }
        if is_video and keyframe_count is not None:
            generation_params["explicit_keyframe_count"] = keyframe_count

        draft = DraftResult(
            draft_id=draft_id,
            previews=previews,
            generation_params=generation_params,
            created_at=now,
            expires_at=expires_at,
            attempt_number=1,
            max_attempts=config.max_regeneration_attempts,
            status=DRAFT_STATUS_PENDING,
        )

        # Store in Redis with TTL
        await self._store_draft(draft, ttl_seconds)

        logger.info(
            "generation_optimization.draft_generator.draft_created",
            extra={
                "draft_id": draft_id,
                "media_type": "video" if is_video else "image",
                "preview_count": len(previews),
                "expires_at": expires_at,
                "request_id": request.request_id,
            },
        )

        return draft

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

        # Simulate upscale to target resolution
        target_resolution = self._get_target_resolution(draft)
        start_time = time.monotonic()

        # Try ComfyUI upscale first, fall back to placeholder
        comfyui_result = await self._upscale_with_comfyui(draft, target_resolution)
        if comfyui_result is not None:
            output_data = comfyui_result
        else:
            output_data = self._simulate_upscale(draft, target_resolution)

        duration_ms = (time.monotonic() - start_time) * 1000.0

        result = UpscaleResult(
            draft_id=draft_id,
            output_data=output_data,
            target_resolution=target_resolution,
            algorithm_used=self._config.upscale_algorithm,
            duration_ms=duration_ms,
        )

        logger.info(
            "generation_optimization.draft_generator.draft_confirmed",
            extra={
                "draft_id": draft_id,
                "target_resolution": target_resolution,
                "algorithm": self._config.upscale_algorithm,
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
        # Draft is always 512x512, so compute scale
        scale_factor = max(target_width / 512, target_height / 512)
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

        # Fallback to placeholder
        return self._generate_image_preview(request, config)

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

    def _generate_image_preview(
        self,
        request: GenerationRequest,
        config: DraftWorkflowConfig,
    ) -> bytes:
        """生成图片预览占位数据.

        如果 ComfyUI 可用，实际预览由 generate_draft 中的 ComfyUI 流程处理。
        此方法提供 fallback 占位数据。

        Args:
            request: 生成请求
            config: Draft 工作流配置

        Returns:
            预览图的 bytes 数据
        """
        width, height = config.draft_resolution
        # Placeholder: generate a minimal PNG-like header + content indicator
        # In production, this calls the actual generation model at draft resolution
        placeholder = (
            f"DRAFT_PREVIEW:image:{width}x{height}:"
            f"prompt={request.prompt[:50]}:"
            f"id={request.request_id}"
        ).encode("utf-8")
        return placeholder

    def _generate_keyframe_previews(
        self,
        request: GenerationRequest,
        config: DraftWorkflowConfig,
        num_keyframes: int,
    ) -> list[bytes]:
        """生成视频关键帧预览占位数据.

        每帧都是 512x512 的占位图像数据。

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
            placeholder = (
                f"DRAFT_PREVIEW:video_keyframe:{width}x{height}:"
                f"frame={i}/{num_keyframes}:"
                f"prompt={request.prompt[:50]}:"
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
            prompt_snippet = old_draft.generation_params.get("prompt", "")[:50]
            placeholder = (
                f"DRAFT_PREVIEW:image:{width}x{height}:"
                f"regenerated:attempt={old_draft.attempt_number + 1}:"
                f"prompt={prompt_snippet}:"
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
    # 内部方法 — Redis 存取
    # ===================================================================

    def _make_redis_key(self, draft_id: str) -> str:
        """构建 Redis 键名.

        格式: aigateway:draft:{draft_id}

        Args:
            draft_id: 草图唯一标识

        Returns:
            Redis 键名
        """
        return f"{_DRAFT_KEY_PREFIX}:{draft_id}"

    async def _store_draft(self, draft: DraftResult, ttl_seconds: int) -> None:
        """将草图存储到 Redis.

        将 DraftResult 序列化为 JSON 存储，previews 数据编码为
        base64 字符串列表。

        Args:
            draft: 草图结果
            ttl_seconds: TTL 秒数
        """
        import base64

        key = self._make_redis_key(draft.draft_id)

        # Serialize: convert previews bytes to base64 strings
        serialized = {
            "draft_id": draft.draft_id,
            "previews_b64": [
                base64.b64encode(p).decode("ascii") for p in draft.previews
            ],
            "generation_params": draft.generation_params,
            "created_at": draft.created_at,
            "expires_at": draft.expires_at,
            "attempt_number": draft.attempt_number,
            "max_attempts": draft.max_attempts,
            "status": draft.status,
        }

        data = json.dumps(serialized)

        if self._redis_client is not None:
            await self._redis_client.set(key, data, ex=ttl_seconds)
        else:
            # In-memory fallback
            self._memory_store[key] = data

    async def _load_draft(self, draft_id: str) -> Optional[DraftResult]:
        """从 Redis 加载草图.

        Args:
            draft_id: 草图唯一标识

        Returns:
            DraftResult 或 None
        """
        import base64

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

        # Deserialize previews from base64
        previews = [
            base64.b64decode(p) for p in data.get("previews_b64", [])
        ]

        return DraftResult(
            draft_id=data["draft_id"],
            previews=previews,
            generation_params=data.get("generation_params", {}),
            created_at=data.get("created_at", 0.0),
            expires_at=data.get("expires_at", 0.0),
            attempt_number=data.get("attempt_number", 1),
            max_attempts=data.get("max_attempts", self._config.max_regeneration_attempts),
            status=data.get("status", DRAFT_STATUS_PENDING),
        )

    async def _delete_draft(self, draft_id: str) -> None:
        """从 Redis 删除草图（立即释放资源）.

        Args:
            draft_id: 草图唯一标识
        """
        key = self._make_redis_key(draft_id)

        if self._redis_client is not None:
            await self._redis_client.delete(key)
        else:
            self._memory_store.pop(key, None)

        logger.debug(
            "generation_optimization.draft_generator.draft_deleted",
            extra={"draft_id": draft_id},
        )
