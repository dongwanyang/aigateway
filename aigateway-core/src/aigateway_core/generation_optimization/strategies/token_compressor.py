"""
Token Compressor Strategy — 视觉 Token 压缩核心逻辑
===================================================

对参考图进行语义级压缩，流程：
1. 前景/背景分割
2. 主体特征提取（CLIP 语义特征 / hash-based fallback）
3. 输出 Feature_Vector

功能:
- 压缩率可配置（默认 50%，范围 20%-90%）
- Feature_Vector 维度不超过 max_vector_dimensions（默认 512）
- 仅支持 PNG/JPEG/WebP/BMP 格式，不支持格式透传原图并记录警告
- 单图超时处理（默认 30 秒），超时透传原图
- 每请求最多 10 张图，单图不超过 20MB
- Token 计算: original = file_size_bytes / 4, compressed = vector_dimensions
- CLIP 模型可用时使用真实语义特征提取，不可用时回退到 hash-based 实现

需求: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import time
from typing import Any, List, Optional

from aigateway_core.generation_optimization.config import TokenCompressorConfig
from aigateway_core.generation_optimization.exceptions import TokenCompressionError
from aigateway_core.generation_optimization.models import CompressionResult
from aigateway_core.integration_configs import CLIPConfig
from aigateway_core.media.types import MediaContent

logger = logging.getLogger(__name__)


class TokenCompressorStrategy:
    """视觉 Token 压缩器 — 对参考图进行语义级压缩.

    通过前景/背景分割和主体特征提取，将参考图压缩为低维
    Feature_Vector，大幅减少输入端 Token 消耗。

    当 CLIP 模型可用时，使用真实的视觉语义特征提取（需求 3.1, 3.2, 3.3）。
    当 CLIP 不可用（包未安装或模型加载失败）时，回退到确定性 hash-based
    实现（需求 3.5）。

    CLIP 模型在初始化时加载一次，跨请求复用（需求 3.6）。

    Attributes:
        _config: Token 压缩配置
        _clip_config: CLIP 配置
        _clip_available: CLIP 模型是否可用
        _clip_model: CLIP 模型实例（或 None）
        _clip_processor: CLIP 处理器实例（或 None）
    """

    def __init__(
        self,
        config: TokenCompressorConfig,
        clip_config: Optional[CLIPConfig] = None,
    ) -> None:
        """初始化 Token Compressor 策略.

        Args:
            config: Token 压缩配置实例
            clip_config: CLIP 配置实例（可选，默认使用 CLIPConfig 默认值）
        """
        self._config = config
        self._clip_config = clip_config or CLIPConfig()
        self._clip_model: Optional[Any] = None
        self._clip_processor: Optional[Any] = None
        self._clip_available: bool = False
        self._clip_loaded: bool = False
        self._device: str = self._clip_config.device

    def _ensure_clip_loaded(self) -> None:
        """延迟加载 CLIP 模型（首次调用时加载，避免阻塞启动）."""
        if self._clip_loaded:
            return
        self._clip_loaded = True
        self._load_clip_model()

    def _load_clip_model(self) -> None:
        """初始化时加载 CLIP 模型（一次性）.

        尝试导入 transformers 包并加载 CLIP 模型和处理器。
        失败时标记为不可用，回退到 hash-based 实现（需求 3.5, 3.6）。
        """
        try:
            from transformers import CLIPModel, CLIPProcessor

            self._clip_model = CLIPModel.from_pretrained(
                self._clip_config.model_name
            )
            self._clip_processor = CLIPProcessor.from_pretrained(
                self._clip_config.model_name
            )
            self._clip_model.to(self._device)
            self._clip_model.eval()
            self._clip_available = True
            logger.info(
                "generation_optimization.token_compressor.clip_loaded",
                extra={
                    "model_name": self._clip_config.model_name,
                    "device": self._device,
                },
            )
        except Exception as exc:
            logger.warning(
                "CLIP 模型加载失败，回退到 hash-based: %s", exc
            )
            self._clip_model = None
            self._clip_processor = None
            self._clip_available = False

    async def compress(
        self,
        image: MediaContent,
        config: TokenCompressorConfig,
    ) -> CompressionResult:
        """压缩单张参考图.

        流程:
        1. 检查图片格式是否支持
        2. 检查图片大小是否超限
        3. 在超时限制内执行压缩（前景/背景分割 → 特征提取）
        4. 计算 token 节省

        Args:
            image: 待压缩的参考图
            config: Token 压缩配置（允许运行时覆盖）

        Returns:
            CompressionResult 包含 feature_vector 和 token 节省信息
        """
        start_time = time.monotonic()

        # Lazy load CLIP model on first use (avoids blocking startup)
        self._ensure_clip_loaded()

        # Check format support
        if not self._is_format_supported(image, config):
            logger.warning(
                "generation_optimization.token_compressor.unsupported_format",
                extra={
                    "mime_type": image.mime_type,
                    "supported_formats": config.supported_formats,
                    "fallback_action": "passthrough",
                },
            )
            return self._create_passthrough_result(image, start_time)

        # Check image size
        if image.size_bytes > config.max_image_size_bytes:
            logger.warning(
                "generation_optimization.token_compressor.size_exceeded",
                extra={
                    "size_bytes": image.size_bytes,
                    "max_size_bytes": config.max_image_size_bytes,
                    "fallback_action": "passthrough",
                },
            )
            return self._create_passthrough_result(image, start_time)

        # Execute compression with timeout
        try:
            result = await asyncio.wait_for(
                self._do_compress(image, config),
                timeout=config.timeout_seconds,
            )
            result.duration_ms = _elapsed_ms(start_time)
            return result

        except asyncio.TimeoutError:
            elapsed = _elapsed_ms(start_time)
            logger.warning(
                "generation_optimization.token_compressor.timeout",
                extra={
                    "reason": "timeout",
                    "fallback_action": "passthrough",
                    "timeout_seconds": config.timeout_seconds,
                    "duration_ms": elapsed,
                },
            )
            return self._create_passthrough_result(image, start_time)

        except TokenCompressionError:
            # Re-raise domain exceptions after logging
            raise

        except Exception as exc:
            elapsed = _elapsed_ms(start_time)
            logger.warning(
                "generation_optimization.token_compressor.error",
                extra={
                    "reason": str(exc),
                    "fallback_action": "passthrough",
                    "duration_ms": elapsed,
                },
            )
            return self._create_passthrough_result(image, start_time)

    async def compress_batch(
        self,
        images: List[MediaContent],
        config: TokenCompressorConfig,
    ) -> List[CompressionResult]:
        """批量压缩参考图.

        验证每请求最大图片数限制后，逐张独立处理，
        每张图的错误不影响其他图的处理。

        Args:
            images: 待压缩的参考图列表
            config: Token 压缩配置

        Returns:
            CompressionResult 列表，与输入顺序一一对应

        Raises:
            TokenCompressionError: 图片数量超过 max_images_per_request 时
        """
        if len(images) > config.max_images_per_request:
            raise TokenCompressionError(
                f"Image count {len(images)} exceeds maximum "
                f"{config.max_images_per_request} per request"
            )

        results: List[CompressionResult] = []
        for image in images:
            result = await self.compress(image, config)
            results.append(result)

        return results

    async def _do_compress(
        self,
        image: MediaContent,
        config: TokenCompressorConfig,
    ) -> CompressionResult:
        """执行实际的压缩逻辑（不含超时包装）.

        当 CLIP 可用时（需求 3.1, 3.2, 3.3）:
        1. 将图像字节解码为 PIL.Image
        2. 使用 CLIPProcessor 预处理
        3. 调用 CLIPModel.get_image_features() 提取语义特征
        4. 截断/投影到 max_vector_dimensions

        当 CLIP 不可用时（需求 3.5）:
        回退到 hash-based 确定性特征生成

        Args:
            image: 待压缩的参考图
            config: Token 压缩配置

        Returns:
            CompressionResult
        """
        # Calculate original token count: file_size_bytes / 4
        original_token_count = image.size_bytes // 4

        # Determine target vector dimensions based on compression ratio
        target_dimensions = self._calculate_target_dimensions(
            original_token_count, config
        )

        # Choose extraction path based on CLIP availability
        if self._clip_available:
            feature_vector = self._extract_features_clip(image, target_dimensions)
        else:
            # Fallback to hash-based extraction
            feature_vector = self._extract_features(image, target_dimensions)

        # Calculate actual compression ratio
        compressed_token_count = len(feature_vector)
        if original_token_count > 0:
            compression_ratio = 1.0 - (
                compressed_token_count / original_token_count
            )
        else:
            compression_ratio = 0.0

        # Clamp compression ratio to valid range
        compression_ratio = max(0.0, min(1.0, compression_ratio))

        return CompressionResult(
            feature_vector=feature_vector,
            original_token_count=original_token_count,
            compressed_token_count=compressed_token_count,
            compression_ratio=compression_ratio,
        )

    def _calculate_target_dimensions(
        self,
        original_token_count: int,
        config: TokenCompressorConfig,
    ) -> int:
        """计算目标 feature vector 维度.

        基于 target_compression_ratio 计算目标压缩后的 token 数，
        但不超过 max_vector_dimensions。

        Args:
            original_token_count: 原始 token 数
            config: 配置

        Returns:
            目标维度数（至少为 1）
        """
        # Target compressed tokens = original * (1 - ratio)
        target_tokens = int(
            original_token_count * (1.0 - config.target_compression_ratio)
        )

        # Clamp to max_vector_dimensions
        target_tokens = min(target_tokens, config.max_vector_dimensions)

        # Ensure at least 1 dimension
        return max(1, target_tokens)

    def _extract_features_clip(
        self,
        image: MediaContent,
        target_dimensions: int,
    ) -> List[float]:
        """从图像中使用 CLIP 提取语义特征向量（需求 3.1, 3.2, 3.3）.

        流程:
        1. 获取图像字节数据（raw_data 或从 source_url 下载）
        2. 解码为 PIL.Image
        3. 使用 CLIPProcessor 预处理
        4. 调用 CLIPModel.get_image_features() 提取特征张量
        5. 转换为 float 列表并截断/投影到 target_dimensions（需求 3.7）

        如果 CLIP 提取过程中出错，回退到 hash-based 实现。

        Args:
            image: 参考图
            target_dimensions: 目标维度数

        Returns:
            长度不超过 target_dimensions 的 float 列表
        """
        try:
            from PIL import Image as PILImage

            # Step 1: Get image bytes
            image_bytes = self._get_image_bytes(image)
            if image_bytes is None:
                logger.warning(
                    "generation_optimization.token_compressor.clip_no_image_data",
                    extra={"fallback_action": "hash_based"},
                )
                return self._extract_features(image, target_dimensions)

            # Step 2: Decode to PIL.Image
            pil_image = PILImage.open(io.BytesIO(image_bytes)).convert("RGB")

            # Step 3: Preprocess with CLIPProcessor
            inputs = self._clip_processor(
                images=pil_image, return_tensors="pt"
            )

            # Move inputs to the same device as the model
            import torch

            inputs = {
                k: v.to(self._device) if isinstance(v, torch.Tensor) else v
                for k, v in inputs.items()
            }

            # Step 4: Extract features
            with torch.no_grad():
                features = self._clip_model.get_image_features(**inputs)

            # Step 5: Convert to list of floats
            feature_list = features[0].cpu().tolist()

            # Truncate/project to target_dimensions (需求 3.7)
            if len(feature_list) > target_dimensions:
                feature_list = feature_list[:target_dimensions]

            return feature_list

        except Exception as exc:
            logger.warning(
                "generation_optimization.token_compressor.clip_extraction_failed: %s, "
                "回退到 hash-based",
                exc,
            )
            return self._extract_features(image, target_dimensions)

    def _get_image_bytes(self, image: MediaContent) -> Optional[bytes]:
        """获取图像的字节数据.

        优先使用 raw_data，若为 None 则尝试从 source_url 下载。

        Args:
            image: 参考图

        Returns:
            图像字节数据，无法获取时返回 None
        """
        if image.raw_data is not None:
            return image.raw_data

        # Try to download from source_url
        if image.source_url:
            try:
                import urllib.request

                with urllib.request.urlopen(
                    image.source_url, timeout=10
                ) as response:
                    return response.read()
            except Exception as exc:
                logger.warning(
                    "generation_optimization.token_compressor.download_failed: %s",
                    exc,
                    extra={"source_url": image.source_url},
                )
                return None

        return None

    def _extract_features(
        self,
        image: MediaContent,
        target_dimensions: int,
    ) -> List[float]:
        """从图像中提取特征向量（占位实现）.

        使用基于图像数据哈希的确定性方法生成 feature vector，
        确保相同输入产生相同输出。

        实际的 ML 推理（CLIP/ViT 等）将在后续集成。

        步骤（模拟）:
        1. 前景/背景分割 — 识别主体区域
        2. 主体特征提取 — 从主体区域提取语义特征
        3. 维度压缩 — 输出目标维度的 feature vector

        Args:
            image: 参考图
            target_dimensions: 目标维度数

        Returns:
            长度为 target_dimensions 的 float 列表
        """
        # Use image content or metadata to generate a deterministic seed
        seed_data = self._get_image_seed(image)
        digest = hashlib.sha256(seed_data).digest()

        # Generate deterministic feature vector from hash
        # Expand the hash to cover all required dimensions
        feature_vector: List[float] = []
        for i in range(target_dimensions):
            # Create per-dimension hash by combining base digest with index
            dim_seed = hashlib.md5(digest + i.to_bytes(4, "little")).digest()
            # Convert first 4 bytes to a float in [-1.0, 1.0] range
            raw_value = int.from_bytes(dim_seed[:4], "little", signed=False)
            normalized = (raw_value / 0xFFFFFFFF) * 2.0 - 1.0
            feature_vector.append(normalized)

        return feature_vector

    def _get_image_seed(self, image: MediaContent) -> bytes:
        """获取图像的确定性种子数据.

        优先使用 raw_data，其次使用 source_url，最后使用
        size_bytes + mime_type 的组合。

        Args:
            image: 参考图

        Returns:
            用于哈希的字节数据
        """
        if image.raw_data:
            return image.raw_data
        if image.source_url:
            return image.source_url.encode("utf-8")
        # Fallback: use size + mime_type as seed
        fallback = f"{image.size_bytes}:{image.mime_type or 'unknown'}"
        return fallback.encode("utf-8")

    def _is_format_supported(
        self,
        image: MediaContent,
        config: TokenCompressorConfig,
    ) -> bool:
        """检查图片格式是否在支持列表中.

        Args:
            image: 待检查的参考图
            config: 配置

        Returns:
            True 如果格式受支持
        """
        if not image.mime_type:
            return False
        return image.mime_type.lower() in [
            fmt.lower() for fmt in config.supported_formats
        ]

    def _create_passthrough_result(
        self,
        image: MediaContent,
        start_time: float,
    ) -> CompressionResult:
        """创建透传结果（不压缩）.

        当格式不支持、大小超限或超时时，返回空的 feature vector，
        compression_ratio 为 1.0（无压缩）。

        Args:
            image: 原始参考图
            start_time: 操作开始时间

        Returns:
            表示透传的 CompressionResult
        """
        original_token_count = image.size_bytes // 4
        return CompressionResult(
            feature_vector=[],
            original_token_count=original_token_count,
            compressed_token_count=original_token_count,
            compression_ratio=1.0,
            duration_ms=_elapsed_ms(start_time),
        )


def _elapsed_ms(start_time: float) -> float:
    """计算从 start_time 到当前的经过毫秒数."""
    return (time.monotonic() - start_time) * 1000.0
