"""
MediaCacheManager — 媒体处理结果缓存 (L4)
==========================================

缓存 MOL 处理结果到 Redis，避免重复处理相同媒体内容。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

from aigateway_core.shared.runtime_values import (
    media_cache_ttl_seconds,
    redis_key_prefix,
)

from .types import MediaContent, MediaType

if TYPE_CHECKING:
    from aigateway_core.shared.redis_client import RedisClientManager

logger = logging.getLogger(__name__)


class MediaCacheManager:
    """媒体缓存管理器 — 缓存 MOL 处理结果，避免重复处理。

    Redis key 前缀来自 ``infrastructure.redis.key_prefixes.media``；未显式配置
    时由配置文件中的服务 namespace 派生。TTL 来自
    ``media_optimization.media_cache_ttl``。
    """

    def __init__(
        self,
        redis_client: RedisClientManager,
        *,
        key_prefix: str | None = None,
        default_ttl: int | None = None,
    ) -> None:
        self._redis = redis_client
        self._key_prefix = (key_prefix or redis_key_prefix("media")).rstrip(":")
        self._default_ttl = (
            int(default_ttl)
            if default_ttl is not None
            else media_cache_ttl_seconds()
        )
        if self._default_ttl <= 0:
            raise ValueError("default_ttl must be positive")

    async def get(
        self, media_type: MediaType, content_hash: str
    ) -> MediaContent | None:
        """查询媒体缓存。"""
        if self._redis is None or self._redis.redis is None:
            return None

        key = f"{self._key_prefix}:{media_type.value}:{content_hash}"
        try:
            raw = await self._redis.redis.get(key)
            if raw is None:
                return None
            return self._deserialize(raw)
        except Exception as exc:
            logger.warning("媒体缓存查询失败: %s", exc)
            return None

    async def set(
        self,
        media_type: MediaType,
        content_hash: str,
        content: MediaContent,
        ttl: int | None = None,
    ) -> None:
        """写入媒体缓存。"""
        if self._redis is None or self._redis.redis is None:
            return

        key = f"{self._key_prefix}:{media_type.value}:{content_hash}"
        try:
            serialized = self._serialize(content)
            effective_ttl = self._default_ttl if ttl is None else int(ttl)
            if effective_ttl <= 0:
                raise ValueError("ttl must be positive")
            await self._redis.redis.set(key, serialized, ex=effective_ttl)
        except Exception as exc:
            logger.warning("媒体缓存写入失败: %s", exc)

    @staticmethod
    def compute_hash(url: str, mime_type: str, config_hash: str) -> str:
        """计算媒体内容的缓存 key hash。"""
        data = f"{url}|{mime_type}|{config_hash}"
        return hashlib.sha256(data.encode()).hexdigest()[:32]

    @staticmethod
    def compute_config_hash(config: dict[str, Any]) -> str:
        """计算配置的 hash（用于缓存键）。"""
        config_str = json.dumps(config, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(config_str.encode()).hexdigest()[:16]

    def _serialize(self, content: MediaContent) -> bytes:
        """序列化 MediaContent。"""
        obj = {
            "media_type": content.media_type.value,
            "extracted_text": content.extracted_text,
            "token_savings": content.token_savings,
            "metadata": content.metadata,
            "source_url": content.source_url,
            "mime_type": content.mime_type,
            "size_bytes": content.size_bytes,
        }
        return json.dumps(obj, ensure_ascii=False).encode("utf-8")

    def _deserialize(self, data: bytes) -> MediaContent:
        """反序列化 MediaContent。"""
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        obj = json.loads(data)
        return MediaContent(
            media_type=MediaType(obj["media_type"]),
            extracted_text=obj.get("extracted_text"),
            token_savings=obj.get("token_savings", 0),
            metadata=obj.get("metadata", {}),
            source_url=obj.get("source_url"),
            mime_type=obj.get("mime_type"),
            size_bytes=obj.get("size_bytes", 0),
        )
