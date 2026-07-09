"""
MediaCacheManager — 媒体处理结果缓存 (L4)
==========================================

缓存 MOL 处理结果到 Redis，避免重复处理相同媒体内容。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

from .types import MediaContent, MediaType

if TYPE_CHECKING:
    from aigateway_core.shared.redis_client import RedisClientManager

logger = logging.getLogger(__name__)


class MediaCacheManager:
    """媒体缓存管理器 — 缓存 MOL 处理结果，避免重复处理。

    缓存策略:
    - Key: media_hash(url + mime_type + pipeline_config_hash)
    - Value: 处理后的 MediaContent 序列化（JSON）
    - TTL: 可配置，默认 7 天

    Redis Key 格式:
    - aigateway:media:image:{hash}
    - aigateway:media:video:{hash}
    - aigateway:media:audio:{hash}
    - aigateway:media:document:{hash}
    """

    KEY_PREFIX = "aigateway:media"
    DEFAULT_TTL = 604800  # 7 days

    def __init__(self, redis_client: "RedisClientManager") -> None:
        self._redis = redis_client

    async def get(
        self, media_type: MediaType, content_hash: str
    ) -> Optional[MediaContent]:
        """查询媒体缓存。"""
        if self._redis is None or self._redis.redis is None:
            return None

        key = f"{self.KEY_PREFIX}:{media_type.value}:{content_hash}"
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
        ttl: Optional[int] = None,
    ) -> None:
        """写入媒体缓存。"""
        if self._redis is None or self._redis.redis is None:
            return

        key = f"{self.KEY_PREFIX}:{media_type.value}:{content_hash}"
        try:
            serialized = self._serialize(content)
            await self._redis.redis.set(key, serialized, ex=ttl or self.DEFAULT_TTL)
        except Exception as exc:
            logger.warning("媒体缓存写入失败: %s", exc)

    @staticmethod
    def compute_hash(url: str, mime_type: str, config_hash: str) -> str:
        """计算媒体内容的缓存 key hash。"""
        data = f"{url}|{mime_type}|{config_hash}"
        return hashlib.sha256(data.encode()).hexdigest()[:32]

    @staticmethod
    def compute_config_hash(config: Dict[str, Any]) -> str:
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
