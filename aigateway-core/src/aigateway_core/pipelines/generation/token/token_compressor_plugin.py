"""
TokenCompressorPlugin — 视觉 Token 压缩插件封装
================================================

将 TokenCompressorStrategy 封装为 PipelineEngine 插件，注册到 PluginRegistry。
在 execute() 中通过 emit_plugin_event 发 TraceEvent,先查询 Feature Cache（命中则跳过压缩），
未命中则压缩后存入缓存，禁用时透传参考图不做修改。
记录 token 节省到请求元数据。

需求: 4.7, 5.2, 5.3, 1.8
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.pipelines.generation._common.config import GenerationOptimizationConfig
from aigateway_core.pipelines.generation._common.models import CompressionResult
from aigateway_core.pipelines.generation.token.feature_cache import (
    FeatureCacheManager,
)
from aigateway_core.pipelines.generation.token.token_compressor import (
    TokenCompressorStrategy,
)
from aigateway_core.prefix.media.types import MediaContent, MediaType

logger = logging.getLogger(__name__)

# 命名空间常量
NS_GENERATION_OPTIMIZATION = "generation_optimization"


class TokenCompressorPlugin:
    """视觉 Token 压缩插件 — 将 TokenCompressorStrategy 封装为 PipelineEngine 插件.

    通过 PluginRegistry 注册后由 PipelineEngine 自动调度执行。
    依赖 intent_evaluator 插件先行执行。

    行为:
    - 禁用时: 透传参考图，不做任何修改 (需求 4.7)
    - 启用时:
      1. 从上下文中提取参考图
      2. 对于有 character_id 的请求:
         - 先查询 Feature Cache (需求 5.2)
         - 缓存命中: 使用缓存向量，自动续期 TTL
         - 缓存未命中: 调用 strategy.compress()，然后存入缓存
      3. 对于无 character_id 的请求: 直接压缩（不走缓存）
      4. 将结果写入 ctx.extra["generation_optimization"]["token_compressor"]
      5. 记录 span 属性（total_savings, compression_count, cache_hits）

    Attributes:
        name: 插件名称 "token_compressor"
        enabled: 是否启用
        depends_on: 依赖的插件列表 ["intent_evaluator"]
    """

    name: str = "token_compressor"
    enabled: bool = True
    depends_on: List[str] = ["intent_evaluator"]

    def __init__(
        self,
        strategy: TokenCompressorStrategy,
        cache: FeatureCacheManager,
        config: GenerationOptimizationConfig,
    ) -> None:
        """初始化 TokenCompressorPlugin.

        Args:
            strategy: TokenCompressorStrategy 实例，负责 Token 压缩核心逻辑
            cache: FeatureCacheManager 实例，负责特征向量缓存读写
            config: 生成优化层主配置实例
        """
        self._strategy = strategy
        self._cache = cache
        self._config = config

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        """执行 Token 压缩.

        流程:
        1. 检查 Token Compressor 是否启用，禁用时直接透传 (需求 4.7)
        2. 创建子 span 用于追踪 (需求 1.8)
        3. 从上下文中提取参考图
        4. 对每张参考图:
           a. 若有 character_id 且缓存启用: 先查 Feature Cache (需求 5.2)
           b. 缓存命中: 使用缓存向量，续期 TTL
           c. 缓存未命中: 调用 strategy.compress()，然后存入缓存
           d. 无 character_id: 直接压缩（不走缓存）
        5. 汇总结果写入 ctx.extra
        6. 记录 span 属性

        Args:
            ctx: 管线上下文

        Returns:
            修改后的管线上下文
        """
        # 检查是否禁用 — 禁用时透传不做修改 (需求 4.7)
        if not self._config.token_compressor.enabled:
            logger.debug(
                "generation_optimization.token_compressor.disabled",
                extra={
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                },
            )
            return ctx

        start_time = time.monotonic()

        try:
            # 从上下文中提取参考图
            reference_images = self._extract_reference_images(ctx)

            if not reference_images:
                # 无参考图，写入空结果
                duration_ms = (time.monotonic() - start_time) * 1000.0
                gen_opt = ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})
                gen_opt["token_compressor"] = {
                    "total_original_tokens": 0,
                    "total_compressed_tokens": 0,
                    "total_savings": 0,
                    "per_image_results": [],
                    "cache_hits": 0,
                    "compression_count": 0,
                    "duration_ms": duration_ms,
                }
                # 无参考图视为 skip,仍发一条 TraceEvent 便于全链路观测
                from aigateway_core.pipelines.generation.registration import (
                    emit_plugin_event,
                )

                emit_plugin_event(ctx, self.name, duration_ms, "skip")
                return ctx

            # 提取请求中的 character_id 和 owner_id
            character_id = self._extract_character_id(ctx)
            model_version = self._config.feature_cache.extraction_model_version

            # owner: group_id (group scope) | user_id (private) | "" (public)
            scope = (ctx.extra.get("cache_scope") or "group") if isinstance(ctx.extra, dict) else "group"
            if scope == "private":
                owner_id = ctx.user_id or ""
            elif scope == "public":
                owner_id = ""
            else:
                owner_id = ctx.extra.get("group_id") or ""  # type: ignore[union-attr]

            # 处理每张参考图
            per_image_results: List[Dict[str, Any]] = []
            total_original_tokens = 0
            total_compressed_tokens = 0
            cache_hits = 0
            compression_count = 0

            for image in reference_images:
                result = await self._process_single_image(
                    image=image,
                    character_id=character_id,
                    owner_id=owner_id,
                    model_version=model_version,
                )

                per_image_results.append(result["record"])
                total_original_tokens += result["original_tokens"]
                total_compressed_tokens += result["compressed_tokens"]
                if result["cache_hit"]:
                    cache_hits += 1
                else:
                    compression_count += 1

            # 计算总节省
            total_savings = total_original_tokens - total_compressed_tokens

            # 计算耗时
            duration_ms = (time.monotonic() - start_time) * 1000.0

            # 写入结果到 ctx.extra
            gen_opt = ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})
            gen_opt["token_compressor"] = {
                "total_original_tokens": total_original_tokens,
                "total_compressed_tokens": total_compressed_tokens,
                "total_savings": total_savings,
                "per_image_results": per_image_results,
                "cache_hits": cache_hits,
                "compression_count": compression_count,
                "duration_ms": duration_ms,
            }

            logger.info(
                "generation_optimization.token_compressor.completed",
                extra={
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                    "total_savings": total_savings,
                    "compression_count": compression_count,
                    "cache_hits": cache_hits,
                    "image_count": len(reference_images),
                    "duration_ms": round(duration_ms, 2),
                },
            )

            # 发 TraceEvent(成功)
            from aigateway_core.pipelines.generation.registration import emit_plugin_event

            emit_plugin_event(ctx, self.name, duration_ms, "ok")

        except Exception as exc:
            duration_ms = (time.monotonic() - start_time) * 1000.0

            # 发 TraceEvent(失败)
            from aigateway_core.pipelines.generation.registration import emit_plugin_event

            emit_plugin_event(ctx, self.name, duration_ms, "error")

            logger.warning(
                "generation_optimization.token_compressor.error",
                extra={
                    "reason": str(exc),
                    "fallback_action": "passthrough",
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                    "duration_ms": round(duration_ms, 2),
                },
            )
            # 故障降级: 写入空结果，不阻断管线
            gen_opt = ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})
            gen_opt["token_compressor"] = {
                "total_original_tokens": 0,
                "total_compressed_tokens": 0,
                "total_savings": 0,
                "per_image_results": [],
                "cache_hits": 0,
                "compression_count": 0,
                "duration_ms": duration_ms,
                "error": str(exc),
            }

        return ctx

    async def _process_single_image(
        self,
        image: MediaContent,
        character_id: Optional[str],
        owner_id: str,
        model_version: str,
    ) -> Dict[str, Any]:
        """处理单张参考图: 缓存查找 → 压缩 → 缓存存储.

        Args:
            image: 待处理的参考图
            character_id: 角色 ID（有值时走缓存逻辑）
            owner_id: 所有者标识 (''=public | group_id=group | user_id=private)
            model_version: 特征提取模型版本

        Returns:
            包含处理结果的字典:
            - record: 写入 per_image_results 的记录
            - original_tokens: 原始 token 数
            - compressed_tokens: 压缩后 token 数
            - cache_hit: 是否命中缓存
        """
        cache_hit = False
        feature_vector: Optional[List[float]] = None

        # 有 character_id 且缓存启用时，先查询缓存 (需求 5.2)
        if character_id and self._config.feature_cache.enabled:
            feature_vector = await self._try_cache_lookup(
                owner_id=owner_id,
                character_id=character_id,
                model_version=model_version,
            )
            if feature_vector is not None:
                cache_hit = True

        if cache_hit and feature_vector is not None:
            # 缓存命中: 使用缓存向量
            original_token_count = image.size_bytes // 4
            compressed_token_count = len(feature_vector)
            compression_ratio = (
                1.0 - (compressed_token_count / original_token_count)
                if original_token_count > 0
                else 0.0
            )

            record = {
                "source": "cache",
                "character_id": character_id,
                "original_token_count": original_token_count,
                "compressed_token_count": compressed_token_count,
                "compression_ratio": compression_ratio,
                "feature_vector_dimensions": len(feature_vector),
            }

            return {
                "record": record,
                "original_tokens": original_token_count,
                "compressed_tokens": compressed_token_count,
                "cache_hit": True,
            }

        # 缓存未命中或无 character_id: 执行压缩
        compression_result: CompressionResult = await self._strategy.compress(
            image=image,
            config=self._config.token_compressor,
        )

        # 压缩成功且有 character_id: 存入缓存
        if (
            character_id
            and self._config.feature_cache.enabled
            and compression_result.feature_vector
        ):
            await self._try_cache_store(
                owner_id=owner_id,
                character_id=character_id,
                model_version=model_version,
                vector=compression_result.feature_vector,
            )

        record = {
            "source": "compressed",
            "character_id": character_id,
            "original_token_count": compression_result.original_token_count,
            "compressed_token_count": compression_result.compressed_token_count,
            "compression_ratio": compression_result.compression_ratio,
            "feature_vector_dimensions": len(compression_result.feature_vector),
            "duration_ms": compression_result.duration_ms,
        }

        return {
            "record": record,
            "original_tokens": compression_result.original_token_count,
            "compressed_tokens": compression_result.compressed_token_count,
            "cache_hit": False,
        }

    async def _try_cache_lookup(
        self,
        owner_id: str,
        character_id: str,
        model_version: str,
    ) -> Optional[List[float]]:
        """尝试从 Feature Cache 查找缓存的特征向量.

        缓存查找失败时不抛异常，返回 None 由调用者决定降级策略。

        Args:
            owner_id: 所有者标识 (''=public | group_id=group | user_id=private)
            character_id: 角色标识符
            model_version: 特征提取模型版本

        Returns:
            缓存的特征向量，未命中或失败时返回 None
        """
        try:
            vector = await self._cache.get_feature(
                owner_id=owner_id,
                character_id=character_id,
                model_version=model_version,
                timeout_ms=self._config.feature_cache.lookup_timeout_ms,
            )
            return vector
        except Exception as exc:
            logger.warning(
                "generation_optimization.token_compressor.cache_lookup_failed",
                extra={
                    "reason": str(exc),
                    "owner_id": owner_id,
                    "character_id": character_id,
                    "fallback_action": "compress_from_original",
                },
            )
            return None

    async def _try_cache_store(
        self,
        owner_id: str,
        character_id: str,
        model_version: str,
        vector: List[float],
    ) -> None:
        """尝试将特征向量存入 Feature Cache.

        存储失败时不抛异常，仅记录警告日志。

        Args:
            owner_id: 所有者标识 (''=public | group_id=group | user_id=private)
            character_id: 角色标识符
            model_version: 特征提取模型版本
            vector: 待存储的特征向量
        """
        try:
            await self._cache.store_feature(
                owner_id=owner_id,
                character_id=character_id,
                model_version=model_version,
                vector=vector,
                ttl_days=self._config.feature_cache.ttl_days,
            )
        except Exception as exc:
            logger.warning(
                "generation_optimization.token_compressor.cache_store_failed",
                extra={
                    "reason": str(exc),
                    "owner_id": owner_id,
                    "character_id": character_id,
                },
            )

    def _extract_reference_images(self, ctx: PipelineContext) -> List[MediaContent]:
        """从上下文中提取参考图列表.

        优先从 media_optimization 命名空间中提取已处理的媒体结果，
        若不存在则从请求的 multimodal content 中提取图片 URL。

        Args:
            ctx: 管线上下文

        Returns:
            MediaContent 列表
        """
        reference_images: List[MediaContent] = []

        # 尝试从 media_optimization 命名空间获取已检测到的图片
        media_opt = ctx.extra.get("media_optimization", {})
        per_media_results = media_opt.get("per_media_results", [])

        for result in per_media_results:
            if isinstance(result, MediaContent):
                if result.media_type == MediaType.IMAGE:
                    reference_images.append(result)
            elif isinstance(result, dict):
                media_type = result.get("media_type", "")
                if media_type in ("image", MediaType.IMAGE):
                    reference_images.append(
                        MediaContent(
                            media_type=MediaType.IMAGE,
                            source_url=result.get("source_url"),
                            mime_type=result.get("mime_type"),
                            size_bytes=result.get("size_bytes", 0),
                            metadata=result.get("metadata", {}),
                        )
                    )

        # 如果 media_optimization 中没有找到图片，尝试从请求中提取
        if not reference_images:
            messages = ctx.request.get("messages", [])
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "image_url":
                                image_url = part.get("image_url", {})
                                url = (
                                    image_url.get("url", "")
                                    if isinstance(image_url, dict)
                                    else ""
                                )
                                if url:
                                    reference_images.append(
                                        MediaContent(
                                            media_type=MediaType.IMAGE,
                                            source_url=url,
                                        )
                                    )
                    break  # 只看最后一条 user message

        return reference_images

    def _extract_character_id(self, ctx: PipelineContext) -> Optional[str]:
        """从请求中提取 character_id.

        查找顺序:
        1. ctx.request["character_id"]
        2. ctx.extra["generation_optimization"]["character_id"]

        Args:
            ctx: 管线上下文

        Returns:
            角色 ID 字符串，无则返回 None
        """
        # 从请求顶层查找
        character_id = ctx.request.get("character_id")
        if character_id:
            return str(character_id)

        # 从 generation_optimization 命名空间查找
        gen_opt = ctx.extra.get(NS_GENERATION_OPTIMIZATION, {})
        character_id = gen_opt.get("character_id")
        if character_id:
            return str(character_id)

        return None
