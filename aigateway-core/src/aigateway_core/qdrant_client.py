"""
Qdrant 连接管理
===============

提供 Qdrant REST API 的异步封装，用于向量存储和语义缓存。

根据 DB_SCHEMA.md Qdrant Collection 结构定义:
- semantic_cache (集合名: semantic_cache)
- rag_documents (预留集合名: rag_documents)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from httpx import AsyncClient, Timeout

logger = logging.getLogger(__name__)


class QdrantClientManager:
    """Qdrant 客户端管理器。

    属性:
        url: Qdrant REST API 地址，例如 "http://localhost:6333"。
        _http: httpx.AsyncClient 实例。
    """

    def __init__(self) -> None:
        self.url: str = "http://localhost:6333"
        self._http: AsyncClient | None = None

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    async def connect(self, url: str = "http://localhost:6333") -> None:
        """连接到 Qdrant REST API。

        Args:
            url: Qdrant 服务器地址。

        Raises:
            ConnectionError: 连接或健康检查失败时抛出。
        """
        self.url = url.rstrip("/")
        self._http = AsyncClient(
            base_url=self.url,
            timeout=Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0),
        )
        # 执行健康检查
        try:
            resp = await self._http.get("/")
            resp.raise_for_status()
            logger.info("Qdrant 连接成功: %s", self.url)
        except Exception as exc:
            await self._http.aclose()
            self._http = None
            raise ConnectionError(
                f"Qdrant 连接失败 ({self.url}): {exc}"
            ) from exc

    async def disconnect(self) -> None:
        """关闭 HTTP 连接。"""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _headers(self) -> Dict[str, str]:
        """返回请求头（支持 API Key 鉴权）。"""
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        api_key = self._api_key_from_env()
        if api_key:
            headers["api-key"] = api_key
        return headers

    @staticmethod
    def _api_key_from_env() -> Optional[str]:
        """从环境变量获取 Qdrant API Key。"""
        import os
        return os.environ.get("QDRANT_API_KEY")

    # ------------------------------------------------------------------
    # 集合管理
    # ------------------------------------------------------------------

    async def upsert_collection(
        self,
        name: str,
        size: int = 384,
        distance: str = "COSINE",
    ) -> bool:
        """创建或确认 Qdrant 集合存在。

        DB_SCHEMA §Qdrant 中定义的向量配置:
        - size=384, distance=COSINE
        - hnsw_config.m=16, ef_construct=128

        Args:
            name: 集合名称，如 "semantic_cache"。
            size: 向量维度，默认 384。
            distance: 距离度量方式，默认 "COSINE"。

        Returns:
            集合是否成功创建或已存在。

        Raises:
            RuntimeError: 未连接 Qdrant 时抛出。
        """
        if self._http is None:
            raise RuntimeError("Qdrant 尚未连接，请先调用 connect()")

        # 先检查集合是否已存在
        existing = await self._http.get("/collections/")
        existing.raise_for_status()
        collections = existing.json().get("result", {}).get("collections", [])
        for coll in collections:
            if coll.get("name") == name:
                logger.info("Qdrant 集合 '%s' 已存在，跳过创建", name)
                return True

        # 创建集合
        payload = {
            "name": name,
            "vectors": {
                "size": size,
                "distance": distance,
            },
            "hnsw_config": {
                "m": 16,
                "ef_construct": 128,
            },
            "optimization_config": {
                "memlock": False,
            },
        }

        resp = await self._http.put(
            f"/collections/{name}",
            json=payload,
            headers=self._headers(),
        )
        resp.raise_for_status()
        result = resp.json().get("result", {})
        logger.info("Qdrant 集合 '%s' 创建成功: %s", name, result)
        return True

    # ------------------------------------------------------------------
    # 向量操作
    # ------------------------------------------------------------------

    async def store_embedding(
        self,
        collection: str,
        payload: Dict[str, Any],
        vector: List[float],
    ) -> str:
        """存储向量及其 Payload 数据到集合。

        DB_SCHEMA §Qdrant 语义缓存集合 Payload Schema:
        - prompt_hash, prompt_normalized, model, response_json, user_id
        - created_at, ttl, hit_count, token_count, cache_tier, embedding_model

        Args:
            collection: 集合名称，如 "semantic_cache"。
            payload: 要附加的负载数据字典。
            vector: 嵌入向量（384 维 float）。

        Returns:
            点（point）的 UUID 字符串 ID。
        """
        if self._http is None:
            raise RuntimeError("Qdrant 尚未连接，请先调用 connect()")

        import uuid

        point_id = str(uuid.uuid4())

        points = [
            {
                "id": point_id,
                "vector": vector,
                "payload": payload,
            }
        ]

        payload_body = {"points": points}

        resp = await self._http.post(
            f"/collections/{collection}/points",
            json=payload_body,
            headers=self._headers(),
        )
        resp.raise_for_status()
        result = resp.json().get("result", {})
        logger.debug("Qdrant 存储成功，point_id=%s, operation=%s", point_id, result.get("operation"))
        return point_id

    async def query_vector(
        self,
        collection: str,
        vector: List[float],
        limit: int = 1,
        score_threshold: float = 0.95,
        user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """向量相似度搜索（L3 缓存查询）。

        DB_SCHEMA §Qdrant 语义缓存集合查询参数:
        - limit 默认 1
        - score_threshold 默认 0.95

        Args:
            collection: 集合名称，如 "semantic_cache"。
            vector: 查询向量。
            limit: 返回结果数量上限，默认 1。
            score_threshold: 最小相似度阈值，默认 0.95。
            user_id: 可选的多租户隔离过滤器。

        Returns:
            包含 points 列表的字典（含 score 和 payload），
            无匹配时返回 None。
        """
        if self._http is None:
            raise RuntimeError("Qdrant 尚未连接，请先调用 connect()")

        query_payload: Dict[str, Any] = {
            "vector": vector,
            "limit": limit,
            "with_payload": True,
            "score_threshold": score_threshold,
        }

        # 多租户隔离：通过 payload 过滤器限定 user_id
        if user_id:
            query_payload["filter"] = {
                "must": [
                    {
                        "key": "user_id",
                        "match": {"value": user_id},
                    }
                ]
            }

        resp = await self._http.post(
            f"/collections/{collection}/points/search",
            json=query_payload,
            headers=self._headers(),
        )
        resp.raise_for_status()
        result = resp.json().get("result", [])

        if not result:
            return None

        # 返回第一条最相似结果
        top = result[0]
        return {
            "id": top.get("id"),
            "score": top.get("score"),
            "payload": top.get("payload", {}),
        }

    async def delete_collection(self, name: str) -> bool:
        """删除 Qdrant 集合（谨慎使用）。

        Args:
            name: 集合名称。

        Returns:
            是否删除成功。
        """
        if self._http is None:
            raise RuntimeError("Qdrant 尚未连接，请先调用 connect()")

        resp = await self._http.delete(f"/collections/{name}")
        resp.raise_for_status()
        logger.info("Qdrant 集合 '%s' 已删除", name)
        return True


# 全局单例（懒初始化）
_qdrant_manager: QdrantClientManager | None = None


def get_qdrant_manager() -> QdrantClientManager:
    """获取全局 Qdrant 客户端管理器单例。"""
    global _qdrant_manager
    if _qdrant_manager is None:
        _qdrant_manager = QdrantClientManager()
    return _qdrant_manager
