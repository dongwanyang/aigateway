"""
三级缓存实现
============

提供 L1（进程内 LRUCache）/ L2（Redis KV）/ L3（Qdrant 向量）
三级缓存架构。

根据 DB_SCHEMA.md:
- §3 缓存键 — L1: cachetools.LRUCache, L2: Redis String LZ4, L3: Qdrant
- In-Memory 数据结构 — L1: LRUCache maxsize=1000

结合 TECH_SPEC.md:
- 进程缓存: cachetools 5.3+ (LRUCache)
- 嵌入模型: sentence-transformers all-MiniLM-L6-v2
- 向量缓存: Qdrant 1.7+
"""

from __future__ import annotations

import hashlib
import logging
import lz4.frame
import threading
import time
from typing import Any, Dict, List, Optional

from cachetools import LRUCache

logger = logging.getLogger(__name__)


class CacheManager:
    """三级缓存管理器。

    L1 — 进程内 LRUCache（ cachetools ）
    L2 — Redis String（ LZ4 压缩）
    L3 — Qdrant 向量相似度搜索
    """

    def __init__(
        self,
        l1_maxsize: int = 1000,
        l2_default_ttl: int = 3600,
        l3_default_ttl: int = 86400,
    ) -> None:
        """
        Args:
            l1_maxsize: L1 缓存最大条目数，默认 1000（TECH_SPEC.md）。
            l2_default_ttl: L2 Redis 缓存默认 TTL（秒），默认 3600。
            l3_default_ttl: L3 Qdrant 缓存默认 TTL（秒），默认 86400。
        """
        # L1: 进程内 LRUCache
        self._l1: LRUCache[str, str] = LRUCache(maxsize=l1_maxsize)
        self._l1_lock = threading.Lock()

        # L2: Redis TTL（秒）
        self.l2_default_ttl = l2_default_ttl

        # L3: Qdrant TTL（秒）
        self.l3_default_ttl = l3_default_ttl

        # L2 和 L3 的客户端（外部注入，避免循环依赖）
        self._redis_client: Any = None  # RedisClientManager
        self._qdrant_client: Any = None  # QdrantClientManager

    def set_redis_client(self, client: Any) -> None:
        """注入 Redis 客户端实例（解耦依赖）。"""
        self._redis_client = client

    def set_qdrant_client(self, client: Any) -> None:
        """注入 Qdrant 客户端实例（解耦依赖）。"""
        self._qdrant_client = client

    # ------------------------------------------------------------------
    # L1: 进程内缓存 (cachetools.LRUCache)
    # ------------------------------------------------------------------

    def l1_get(self, key: str) -> Optional[str]:
        """从 L1 缓存读取。

        DB_SCHEMA §3: Key 格式为 SHA-256(normalized_prompt + model + params)

        Args:
            key: 缓存键（SHA-256 哈希字符串）。

        Returns:
            缓存值（JSON 字符串），未命中返回 None。
        """
        with self._l1_lock:
            value = self._l1.get(key)
            if value is not None:
                logger.debug("L1 缓存命中: key=%s...", key[:16])
            return value

    def l1_set(self, key: str, value: str, ttl: Optional[int] = None) -> None:
        """写入 L1 缓存。

        Args:
            key: 缓存键（SHA-256 哈希）。
            value: 完整 OpenAI 格式响应 JSON 字符串。
            ttl: 生存时间（秒），L1 使用 LRU 淘汰，此参数忽略。
        """
        with self._l1_lock:
            self._l1[key] = value
            logger.debug("L1 缓存写入: key=%s", key[:16])

    # ------------------------------------------------------------------
    # L2: Redis KV 缓存 (LZ4 压缩)
    # ------------------------------------------------------------------

    def _compress(self, data: str) -> bytes:
        """使用 LZ4 压缩数据。

        DB_SCHEMA §3: L2 缓存使用 LZ4 压缩存储
        """
        return lz4.frame.compress(
            data.encode("utf-8"),
            compression_level=9,
            store_size=False,
        )

    def _decompress(self, data: bytes) -> str:
        """解压 LZ4 压缩数据。"""
        return lz4.frame.decompress(data).decode("utf-8")

    async def l2_get(self, key: str) -> Optional[str]:
        """从 L2 Redis 缓存读取。

        DB_SCHEMA §3: Key 格式 aigateway:cache:v1:{cache_key_hash}

        Args:
            key: 缓存键（SHA-256 哈希）。

        Returns:
            响应 JSON 字符串，未命中返回 None。
        """
        if self._redis_client is None:
            logger.warning("L2 缓存: Redis 客户端未初始化")
            return None

        redis_key = f"aigateway:cache:v1:{key}"
        raw = await self._redis_client.redis.get(redis_key)
        if raw is None:
            logger.debug("L2 缓存未命中: key=%s", key[:16])
            return None

        try:
            decompressed = self._decompress(raw)
            logger.debug("L2 缓存命中: key=%s", key[:16])
            return decompressed
        except Exception as exc:
            logger.error("L2 缓存解压失败: key=%s, error=%s", key[:16], exc)
            return None

    async def l2_set(self, key: str, value: str, ttl: Optional[int] = None) -> None:
        """写入 L2 Redis 缓存。

        DB_SCHEMA §3:
        - Key 格式: aigateway:cache:v1:{cache_key_hash}
        - TTL 默认 3600 秒，由 prompt_cache.config.ttl 决定
        - 压缩: LZ4

        Args:
            key: 缓存键（SHA-256 哈希）。
            value: 完整 OpenAI 格式响应 JSON 字符串。
            ttl: 生存时间（秒），默认为 l2_default_ttl。
        """
        if self._redis_client is None:
            logger.warning("L2 缓存: Redis 客户端未初始化")
            return

        ttl = ttl or self.l2_default_ttl
        redis_key = f"aigateway:cache:v1:{key}"

        compressed = self._compress(value)

        # 同时存储结构化元数据作为单独字段，用于 hit_count 追踪
        # 但 DB_SCHEMA 中 Value 结构为压缩后的字节串，这里采用单 Key 方案
        await self._redis_client.redis.set(redis_key, compressed, ex=ttl)
        logger.debug("L2 缓存写入: key=%s ttl=%ds", key[:16], ttl)

    # ------------------------------------------------------------------
    # L3: Qdrant 向量缓存
    # ------------------------------------------------------------------

    async def l3_query(
        self,
        vector: List[float],
        threshold: float = 0.95,
        limit: int = 1,
        user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Qdrant 向量相似度搜索（L3 语义缓存）。

        DB_SCHEMA §Qdrant 语义缓存集合查询参数:
        - limit 默认 1
        - score_threshold 默认 0.95

        Args:
            vector: 归一化 prompt 的嵌入向量（384 维）。
            threshold: 相似度阈值，默认 0.95。
            limit: 返回结果数上限，默认 1。
            user_id: 多租户隔离的用户 ID。

        Returns:
            匹配结果字典 {id, score, payload}，未命中返回 None。
        """
        if self._qdrant_client is None:
            logger.warning("L3 缓存: Qdrant 客户端未初始化")
            return None

        collection = "semantic_cache"

        result = await self._qdrant_client.query_vector(
            collection=collection,
            vector=vector,
            limit=limit,
            score_threshold=threshold,
            user_id=user_id,
        )

        if result is None:
            logger.debug("L3 缓存未命中: 无符合阈值的相似向量")
            return None

        # 从 payload 中提取缓存响应
        payload = result.get("payload", {})
        response_json = payload.get("response_json", "")

        # 检查 TTL 是否过期（防御性检查）
        ttl_expire = payload.get("ttl", 0)
        now = int(time.time())
        if ttl_expire > 0 and now > ttl_expire:
            logger.debug("L3 缓存结果已过期: ttl_expire=%d now=%d", ttl_expire, now)
            return None

        # 命中次数 +1
        hit_count = payload.get("hit_count", 0) + 1
        payload["hit_count"] = hit_count
        await self._qdrant_client.store_embedding(
            collection=collection,
            payload=payload,
            vector=vector,
        )

        logger.debug("L3 缓存命中: score=%.4f", result.get("score", 0))
        return {
            "id": result["id"],
            "score": result.get("score"),
            "response_json": response_json,
            "model": payload.get("model"),
            "hit_count": hit_count,
        }

    async def l3_store(
        self,
        prompt_hash: str,
        prompt_normalized: str,
        model: str,
        response_json: str,
        user_id: str,
        token_count: int,
        vector: List[float],
        ttl: Optional[int] = None,
        embedding_model: str = "all-MiniLM-L6-v2",
    ) -> None:
        """将缓存结果存储到 L3 Qdrant。

        DB_SCHEMA §Qdrant 语义缓存集合 Payload Schema
        """
        if self._qdrant_client is None:
            logger.warning("L3 缓存: Qdrant 客户端未初始化")
            return

        now = int(time.time())
        ttl_seconds = ttl or self.l3_default_ttl

        payload: Dict[str, Any] = {
            "prompt_hash": prompt_hash,
            "prompt_normalized": prompt_normalized,
            "model": model,
            "response_json": response_json,
            "user_id": user_id,
            "created_at": now,
            "ttl": now + ttl_seconds,
            "hit_count": 0,
            "token_count": token_count,
            "cache_tier": "L3",
            "embedding_model": embedding_model,
        }

        await self._qdrant_client.store_embedding(
            collection="semantic_cache",
            payload=payload,
            vector=vector,
        )
        logger.debug("L3 缓存写入: prompt_hash=%s model=%s", prompt_hash[:16], model)

    # ------------------------------------------------------------------
    # 缓存键生成
    # ------------------------------------------------------------------

    @staticmethod
    def generate_cache_key(
        normalized_prompt: str,
        model: str,
        **params: Any,
    ) -> str:
        """生成缓存键（SHA-256）。

        DB_SCHEMA §3:
        L1 Key = SHA-256(normalized_messages_json + model_name + temperature +
                           max_tokens + top_p + user_id)
        L2 Key = aigateway:cache:v1:{SHA-256 相同规则}

        Args:
            normalized_prompt: 归一化后的 prompt 文本。
            model: 模型名称。
            **params: 其他参数 (temperature, max_tokens, top_p, user_id 等)。

        Returns:
            64 位 hex SHA-256 哈希字符串。
        """
        # 按固定顺序拼接参数以确保键的一致性
        parts: List[str] = [
            normalized_prompt,
            model,
        ]
        # 对参数按 key 排序以确保确定性
        for param_key in sorted(params.keys()):
            param_val = params[param_key]
            if param_val is not None:
                parts.append(str(param_key))
                parts.append(str(param_val))

        key_string = "|".join(parts)
        return hashlib.sha256(key_string.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # 多级缓存联动
    # ------------------------------------------------------------------

    async def get(self, key: str, value_fn, **params: Any) -> Optional[Dict[str, Any]]:
        """多级缓存穿透查询。

        依次检查 L1 -> L2 -> L3，逐级穿透后调用 value_fn 获取并回填。

        Args:
            key: 缓存键。
            value_fn: 未命中时的回调函数，返回 (value_str, extra_meta)。
            **params: 传递给 value_fn 的额外参数。

        Returns:
            缓存结果字典 {hit_tier, value, meta} 或 None。
        """
        # L1 查询
        cached = self.l1_get(key)
        if cached is not None:
            return {"hit_tier": "L1", "value": cached, "meta": params}

        # L2 查询
        l2_value = await self.l2_get(key)
        if l2_value is not None:
            # 命中的同时回填 L1
            self.l1_set(key, l2_value)
            return {"hit_tier": "L2", "value": l2_value, "meta": params}

        # L3 查询（语义缓存，需要先计算向量）
        # 此处假设调用方已通过 embedding 获取向量
        if "vector" in params:
            l3_result = await self.l3_query(
                vector=params["vector"],
                threshold=params.get("threshold", 0.95),
                user_id=params.get("user_id"),
            )
            if l3_result is not None:
                response_json = l3_result.get("response_json", "")
                if response_json:
                    self.l1_set(key, response_json)
                    return {"hit_tier": "L3", "value": response_json, "meta": l3_result}

        # 三级均未命中，调用 value_fn 获取数据
        if callable(value_fn):
            result = value_fn(**params)
            if result is not None:
                value_str, extra = result if isinstance(result, tuple) else (result, {})
                # 回填 L1
                self.l1_set(key, value_str)
                # 回填 L2（带 TTL）
                l2_ttl = extra.get("l2_ttl", self.l2_default_ttl)
                await self.l2_set(key, value_str, ttl=l2_ttl)
                # 回填 L3（如果有向量）
                if "vector" in extra:
                    l3_ttl = extra.get("l3_ttl", self.l3_default_ttl)
                    await self.l3_store(
                        prompt_hash=params.get("prompt_hash", ""),
                        prompt_normalized=params.get("normalized_prompt", ""),
                        model=params.get("model", ""),
                        response_json=value_str,
                        user_id=params.get("user_id", ""),
                        token_count=extra.get("token_count", 0),
                        vector=extra["vector"],
                        ttl=l3_ttl,
                    )
                return {"hit_tier": "MISS", "value": value_str, "meta": extra}

        return None


# End of CacheManager
