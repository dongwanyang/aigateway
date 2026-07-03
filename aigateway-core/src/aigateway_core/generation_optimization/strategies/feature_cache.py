"""
FeatureCacheManager — 特征向量缓存管理器
========================================

管理角色 Feature Vector 的存取和复用，复用现有 Redis 基础设施。

Redis Key 格式:
    aigateway:feature:{api_key_id}:{character_id}:{model_version}

缓存行为:
- 缓存查找超时 500ms（可配置），防止 Redis 延迟影响请求
- 缓存命中时自动续期 TTL
- 缓存以 API Key 隔离，不同 API Key 同名 character_id 不冲突
- 缓存失败时降级到从原始图重新提取（由调用者处理）

需求: 5.1, 5.2, 5.4, 5.5, 5.6, 5.7
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, List, Optional

from aigateway_core.generation_optimization.config import FeatureCacheConfig

logger = logging.getLogger(__name__)


class FeatureCacheManager:
    """特征缓存管理器 — 管理角色 Feature Vector 的存取和复用.

    复用现有 Redis 基础设施，Key 格式:
    aigateway:feature:{api_key_id}:{character_id}:{model_version}

    用法:
        cache = FeatureCacheManager(redis_client, config)
        vector = await cache.get_feature("key123", "char_01", "clip-vit-large-patch14")
        if vector is None:
            vector = await extract_feature(...)
            await cache.store_feature("key123", "char_01", "clip-vit-large-patch14", vector)
    """

    KEY_PREFIX = "aigateway:feature"

    def __init__(self, redis_client: Any, config: FeatureCacheConfig) -> None:
        """初始化特征缓存管理器.

        Args:
            redis_client: RedisClientManager 实例，需有 .redis 属性提供异步 Redis 方法。
            config: FeatureCacheConfig 配置实例。
        """
        self._redis_client = redis_client
        self._config = config

    def _build_key(self, api_key_id: str, character_id: str, model_version: str) -> str:
        """构建 Redis 缓存 Key.

        格式: aigateway:feature:{api_key_id}:{character_id}:{model_version}

        通过 api_key_id 作为 Key 的一部分，确保不同 API Key 的同名
        character_id 不会冲突（需求 5.7）。

        Args:
            api_key_id: API Key 标识符
            character_id: 角色标识符
            model_version: 特征提取模型版本

        Returns:
            完整的 Redis Key 字符串
        """
        return f"{self.KEY_PREFIX}:{api_key_id}:{character_id}:{model_version}"

    async def get_feature(
        self,
        api_key_id: str,
        character_id: str,
        model_version: str,
        timeout_ms: int = 500,
    ) -> Optional[List[float]]:
        """查询缓存的特征向量.

        在指定超时时间内从 Redis 获取缓存的 Feature Vector。
        命中时自动续期 TTL（需求 5.4）。
        Redis 不可用或超时时返回 None，由调用者决定降级策略。

        Args:
            api_key_id: API Key 标识符
            character_id: 角色标识符
            model_version: 特征提取模型版本
            timeout_ms: 缓存查找超时毫秒数 (默认: 500)

        Returns:
            缓存的特征向量列表，未命中或失败时返回 None
        """
        key = self._build_key(api_key_id, character_id, model_version)

        try:
            redis = self._redis_client.redis
            if redis is None:
                logger.warning(
                    "feature_cache.get_feature: Redis 未连接，跳过缓存查找",
                    extra={"api_key_id": api_key_id, "character_id": character_id},
                )
                return None

            # 使用 asyncio.wait_for 实现超时控制（需求 5.2）
            timeout_seconds = timeout_ms / 1000.0
            raw = await asyncio.wait_for(redis.get(key), timeout=timeout_seconds)

            if raw is None:
                return None

            # 反序列化 JSON → List[float]
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            vector: List[float] = json.loads(raw)

            # 缓存命中，自动续期 TTL（需求 5.4）
            # 异步续期，不阻塞返回
            asyncio.ensure_future(
                self.extend_ttl(api_key_id, character_id, model_version)
            )

            return vector

        except asyncio.TimeoutError:
            logger.warning(
                "feature_cache.get_feature: 缓存查找超时 (%dms)",
                timeout_ms,
                extra={"api_key_id": api_key_id, "character_id": character_id, "key": key},
            )
            return None
        except Exception as exc:
            logger.warning(
                "feature_cache.get_feature: 缓存查找失败: %s",
                exc,
                extra={"api_key_id": api_key_id, "character_id": character_id, "key": key},
            )
            return None

    async def store_feature(
        self,
        api_key_id: str,
        character_id: str,
        model_version: str,
        vector: List[float],
        ttl_days: int = 30,
    ) -> None:
        """存储特征向量到缓存.

        将 Feature Vector 序列化为 JSON 并存储到 Redis，设置 TTL。

        Args:
            api_key_id: API Key 标识符
            character_id: 角色标识符
            model_version: 特征提取模型版本
            vector: 特征向量
            ttl_days: 缓存 TTL 天数 (默认: 30)
        """
        key = self._build_key(api_key_id, character_id, model_version)

        try:
            redis = self._redis_client.redis
            if redis is None:
                logger.warning(
                    "feature_cache.store_feature: Redis 未连接，跳过缓存存储",
                    extra={"api_key_id": api_key_id, "character_id": character_id},
                )
                return

            # 序列化 vector 为 JSON
            serialized = json.dumps(vector)
            ttl_seconds = ttl_days * 86400

            await redis.set(key, serialized, ex=ttl_seconds)

        except Exception as exc:
            logger.warning(
                "feature_cache.store_feature: 缓存存储失败: %s",
                exc,
                extra={"api_key_id": api_key_id, "character_id": character_id, "key": key},
            )

    async def extend_ttl(
        self,
        api_key_id: str,
        character_id: str,
        model_version: str,
        ttl_days: int = 30,
    ) -> None:
        """续期缓存 TTL.

        对已存在的缓存条目延长 TTL（需求 5.4）。

        Args:
            api_key_id: API Key 标识符
            character_id: 角色标识符
            model_version: 特征提取模型版本
            ttl_days: 续期 TTL 天数 (默认: 30)
        """
        key = self._build_key(api_key_id, character_id, model_version)

        try:
            redis = self._redis_client.redis
            if redis is None:
                return

            ttl_seconds = ttl_days * 86400
            await redis.expire(key, ttl_seconds)

        except Exception as exc:
            logger.warning(
                "feature_cache.extend_ttl: TTL 续期失败: %s",
                exc,
                extra={"api_key_id": api_key_id, "character_id": character_id, "key": key},
            )
