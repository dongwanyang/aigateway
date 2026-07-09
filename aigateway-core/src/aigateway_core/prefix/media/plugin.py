"""
MediaOptimizationPlugin — MOL 插件封装
========================================

将 MediaOptimizationLayer 封装为 Pipeline Plugin，
使其能在现有 PipelineEngine 中按序执行。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

from .config import (
    AudioPipelineConfig,
    DocumentPipelineConfig,
    ImagePipelineConfig,
    MediaOptimizationConfig,
    VideoPipelineConfig,
)
from .mol import MediaOptimizationLayer
from .pipelines import AudioPipeline, DocumentPipeline, ImagePipeline, VideoPipeline
from .types import MediaType

if TYPE_CHECKING:
    from aigateway_core.dispatch.context import PipelineContext
    from .cache import MediaCacheManager

logger = logging.getLogger(__name__)


class MediaOptimizationPlugin:
    """MOL Pipeline 插件。

    在 PII Detector 之后、Cache Lookup 之前执行。
    检测并处理请求中的多模态内容。
    """

    name: str = "media_optimizer"
    enabled: bool = True
    depends_on: list = ["pii_detector"]

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        media_cache: Optional["MediaCacheManager"] = None,
    ) -> None:
        cfg_dict = config or {}
        self._config = self._build_config(cfg_dict)
        self._mol: Optional[MediaOptimizationLayer] = None
        self._media_cache = media_cache

        if self._config.enabled:
            self._initialize_mol()

    def _build_config(self, cfg_dict: Dict[str, Any]) -> MediaOptimizationConfig:
        """从 dict 构建配置。"""
        image_cfg = ImagePipelineConfig(**cfg_dict.get("image", {}))
        video_cfg = VideoPipelineConfig(**cfg_dict.get("video", {}))
        audio_cfg = AudioPipelineConfig(**cfg_dict.get("audio", {}))
        doc_cfg = DocumentPipelineConfig(**cfg_dict.get("document", {}))

        return MediaOptimizationConfig(
            enabled=cfg_dict.get("enabled", True),
            image=image_cfg,
            video=video_cfg,
            audio=audio_cfg,
            document=doc_cfg,
            media_cache_ttl=cfg_dict.get("media_cache_ttl", 604800),
            max_concurrent_processors=cfg_dict.get("max_concurrent_processors", 4),
        )

    def _initialize_mol(self) -> None:
        """初始化 Media Optimization Layer。"""
        pipelines = {
            MediaType.IMAGE: ImagePipeline(config=self._config.image),
            MediaType.VIDEO: VideoPipeline(config=self._config.video),
            MediaType.AUDIO: AudioPipeline(config=self._config.audio),
            MediaType.DOCUMENT: DocumentPipeline(config=self._config.document),
        }
        self._mol = MediaOptimizationLayer(
            pipelines=pipelines,
            media_cache=self._media_cache,
            max_concurrent=self._config.max_concurrent_processors,
        )

    async def execute(self, ctx: "PipelineContext") -> "PipelineContext":
        """执行 MOL 处理。"""
        if not self._config.enabled or self._mol is None:
            return ctx

        messages = ctx.request.get("messages", [])
        if not messages:
            return ctx

        # 检测是否包含多模态内容
        has_multimodal = any(
            isinstance(m.get("content"), list) for m in messages
        )
        if not has_multimodal:
            return ctx

        # 执行 MOL
        try:
            optimized_messages = await self._mol.process_messages(messages, ctx)
            ctx.request = {**ctx.request, "messages": optimized_messages}
            ctx.is_multimodal = True

            # 更新 token savings
            mol_ns = ctx.extra.get("media_optimization", {})
            ctx.total_token_savings = mol_ns.get("total_savings", 0)

            logger.info(
                "MOL 处理完成: detected_types=%s, token_savings=%d, request_id=%s",
                mol_ns.get("detected_types", []),
                ctx.total_token_savings,
                ctx.request_id,
            )
        except Exception as exc:
            logger.error(
                "MOL 处理异常 (降级为 passthrough): %s, request_id=%s",
                exc,
                ctx.request_id,
            )

        return ctx
