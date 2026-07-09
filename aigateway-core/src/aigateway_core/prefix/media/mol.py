"""
MediaOptimizationLayer — 多模态处理入口
========================================

职责：
1. 检测消息中的媒体类型
2. 检查媒体缓存
3. 分发到对应 MediaPipeline
4. 缓存处理结果
5. 聚合处理结果回 PipelineContext
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .base import MediaPipeline
from .cache import MediaCacheManager
from .detector import ContentTypeDetector
from .types import MediaContent, MediaType

if TYPE_CHECKING:
    from aigateway_core.dispatch.context import PipelineContext

logger = logging.getLogger(__name__)


class MediaOptimizationLayer:
    """Media Optimization Layer — 多模态处理入口。

    职责：
    1. 检测消息中的媒体类型
    2. 分发到对应 MediaPipeline
    3. 聚合处理结果回 PipelineContext
    """

    def __init__(
        self,
        pipelines: Dict[MediaType, MediaPipeline],
        media_cache: Optional[MediaCacheManager] = None,
        max_concurrent: int = 4,
    ) -> None:
        self._pipelines = pipelines
        self._detector = ContentTypeDetector()
        self._media_cache = media_cache
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def process_messages(
        self,
        messages: List[Dict[str, Any]],
        ctx: "PipelineContext",
    ) -> List[Dict[str, Any]]:
        """处理消息列表中的所有媒体内容。

        Args:
            messages: OpenAI 格式消息列表。
            ctx: Pipeline 上下文。

        Returns:
            优化后的消息列表。
        """
        optimized_messages = []
        total_savings = 0
        detected_types: List[str] = []
        processors_executed: List[str] = []

        for message in messages:
            optimized, savings, types, procs = await self._process_message(
                message, ctx
            )
            optimized_messages.append(optimized)
            total_savings += savings
            detected_types.extend(types)
            processors_executed.extend(procs)

        # 更新 context
        ctx.extra.setdefault("media_optimization", {})
        ctx.extra["media_optimization"]["detected_types"] = list(set(detected_types))
        ctx.extra["media_optimization"]["total_savings"] = total_savings
        ctx.extra["media_optimization"]["processors_executed"] = processors_executed

        return optimized_messages

    async def _process_message(
        self,
        message: Dict[str, Any],
        ctx: "PipelineContext",
    ) -> tuple:
        """处理单条消息。

        Returns:
            (optimized_message, token_savings, detected_types, processors)
        """
        content = message.get("content", "")
        total_savings = 0
        detected_types: List[str] = []
        processors_executed: List[str] = []

        if isinstance(content, str):
            return message, 0, [], []

        if isinstance(content, list):
            optimized_parts = []
            for part in content:
                media_content = self._detector.detect(part)

                if media_content.media_type == MediaType.TEXT:
                    optimized_parts.append(part)
                    continue

                detected_types.append(media_content.media_type.value)

                # 尝试缓存查找
                cached = await self._cache_lookup(media_content)
                if cached is not None:
                    optimized_parts.append(self._to_content_part(cached, fallback=part))
                    total_savings += cached.token_savings
                    continue

                # 执行对应 Pipeline
                pipeline = self._pipelines.get(media_content.media_type)
                if pipeline:
                    start = time.monotonic()
                    async with self._semaphore:
                        try:
                            processed = await pipeline.execute(media_content, ctx)
                            elapsed_ms = (time.monotonic() - start) * 1000
                            processors_executed.append(
                                f"{media_content.media_type.value}:{elapsed_ms:.1f}ms"
                            )
                            # 缓存处理结果
                            await self._cache_store(media_content, processed)
                            new_part = self._to_content_part(processed, fallback=part)
                            optimized_parts.append(new_part)
                            total_savings += processed.token_savings
                        except Exception as exc:
                            logger.warning(
                                "Media pipeline 执行失败 (%s): %s，原样透传",
                                media_content.media_type.value,
                                exc,
                            )
                            optimized_parts.append(part)
                else:
                    # 不支持的类型，透传
                    optimized_parts.append(part)

            return {**message, "content": optimized_parts}, total_savings, detected_types, processors_executed

        return message, 0, [], []

    async def _cache_lookup(self, content: MediaContent) -> Optional[MediaContent]:
        """查找媒体缓存。"""
        if self._media_cache is None:
            return None

        cache_key = self._compute_cache_key(content)
        if cache_key is None:
            return None

        return await self._media_cache.get(content.media_type, cache_key)

    async def _cache_store(
        self, original: MediaContent, processed: MediaContent
    ) -> None:
        """存储处理结果到缓存。"""
        if self._media_cache is None:
            return

        cache_key = self._compute_cache_key(original)
        if cache_key is None:
            return

        await self._media_cache.set(original.media_type, cache_key, processed)

    def _compute_cache_key(self, content: MediaContent) -> Optional[str]:
        """计算缓存 key。"""
        if content.source_url:
            identifier = content.source_url
        elif content.raw_data:
            identifier = hashlib.md5(content.raw_data).hexdigest()
        else:
            return None

        return MediaCacheManager.compute_hash(
            url=identifier,
            mime_type=content.mime_type or "",
            config_hash="default",
        )

    @staticmethod
    def _to_content_part(
        content: MediaContent, fallback: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """将处理后的 MediaContent 转换回 OpenAI ContentPart 格式。

        Args:
            content: 处理后的媒体内容。
            fallback: 无法提取文本时回退的原始 ContentPart（保证不丢失内容）。
        """
        if content.extracted_text:
            return {"type": "text", "text": content.extracted_text}
        if content.optimized_data and content.source_url:
            return {
                "type": "image_url",
                "image_url": {"url": content.source_url},
            }
        # 降级：无法优化时保留原始 part，避免内容丢失（Property 1 & 4）
        if fallback is not None:
            return fallback
        return {"type": "text", "text": ""}
