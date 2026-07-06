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

容量保护策略:
- L1: LRU 自动淘汰 + 单条 ≤ 100KB
- L2: Redis maxmemory + TTL + 单条 ≤ 500KB
- L3: 定期清理过期向量 + token_count 阈值过滤

回填策略:
- L2 命中 → 回填 L1
- L3 命中 → 仅回填 L1（不回填 L2，因 L3 是语义近似匹配）
- MISS → 回填 L1 + L2 + 有条件回填 L3
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import lz4.frame
import re
import threading
import time
import unicodedata
from typing import Any, Awaitable, Callable, Dict, List, Optional

from cachetools import LRUCache

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Cache key v2 常量
# ------------------------------------------------------------------
# 参数分桶:粗粒度合并,让 SDK 默认值细微偏差落到同一 bucket
_TEMPERATURE_BUCKETS: List[tuple] = [
    (0.05, "exact_zero"),   # <= 0.05 视为确定性,单独一桶
    (0.3,  "det"),          # 0.05 ~ 0.3 确定性偏低
    (0.9,  "bal"),          # 0.3 ~ 0.9 平衡
    (float("inf"), "cre"),  # > 0.9 创意
]
_MAX_TOKENS_BUCKETS: List[int] = [256, 512, 1024, 2048, 4096, 8192, 16384]

# model_family:去掉尾部日期 snapshot,如 gpt-4o-2024-08-06 → gpt-4o,
# claude-3-5-sonnet-20241022 → claude-3-5-sonnet。
# 匹配:
#   -YYYYMMDD           如 20241022
#   -YYYY-MM-DD         如 2024-08-06
#   -latest             如 gpt-4-latest(有些厂商)
_MODEL_SNAPSHOT_RE = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2}|latest)$")


def _bucket_temperature(t: Optional[float]) -> str:
    """把 temperature 映射到粗粒度桶。None 视为 1.0(OpenAI 默认)。"""
    if t is None:
        t = 1.0
    for upper, name in _TEMPERATURE_BUCKETS:
        if t <= upper:
            return name
    return "cre"


def _bucket_max_tokens(mt: Optional[int]) -> str:
    """把 max_tokens 映射到就近的分桶。None / 0 → any。"""
    if not mt or mt <= 0:
        return "any"
    # 就近向上取,超过最大档 → 最大档
    for edge in _MAX_TOKENS_BUCKETS:
        if mt <= edge:
            return f"le_{edge}"
    return f"gt_{_MAX_TOKENS_BUCKETS[-1]}"


def _model_family(model: str) -> str:
    """从 model_id 提取 family,去掉尾部日期 snapshot。

    - gpt-4o                       → gpt-4o
    - gpt-4o-2024-08-06            → gpt-4o
    - gpt-4o-mini-2024-07-18       → gpt-4o-mini
    - claude-3-5-sonnet-20241022   → claude-3-5-sonnet
    - claude-sonnet-4-5-20250929   → claude-sonnet-4-5
    - auto                         → auto(特殊值不动)
    - openai/gpt-4o                → openai/gpt-4o(provider 前缀保留)
    """
    if not model:
        return ""
    # 保留 provider/ 前缀,只处理 model 部分
    if "/" in model:
        prefix, tail = model.rsplit("/", 1)
        return f"{prefix}/{_MODEL_SNAPSHOT_RE.sub('', tail)}"
    return _MODEL_SNAPSHOT_RE.sub("", model)


def _normalize_prompt(text: str) -> str:
    """归一化 prompt 文本:NFKC + 折叠空白 + strip。

    - NFKC:全半角、组合字符统一(如 "ａ" → "a",带音标字符合并)
    - 多个连续空白(含制表/换行)折叠为一个空格
    - 首尾空白去除

    目的:让语义等价但格式细微差异的 prompt 生成同一 hash。
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized

# ------------------------------------------------------------------
# 容量保护常量
# ------------------------------------------------------------------

L1_MAX_VALUE_BYTES = 102400    # 单条最大 100KB，超过不进 L1
L2_MAX_VALUE_BYTES = 512000    # 单条最大 500KB，超过不进 L2
L3_MIN_TOKEN_COUNT = 100       # 仅 token_count > 100 的请求才写入 L3
L3_DEFAULT_TTL = 86400         # 24 小时过期
L3_CLEANUP_INTERVAL = 3600     # 每小时清理一次过期向量


def _emit_cache_debug(key: str, tier_hit: str, start_monotonic: float,
                      status: str = "ok") -> None:
    """若 cache 维度 debug 开关开启,发一条 kind=debug TraceEvent.

    在 CacheManager.get 的各命中/MISS 路径调用,key 用 hash 截断避免泄露 prompt 内容。
    """
    from aigateway_core.trace_event import TraceCollector
    import time as _time
    collector = TraceCollector.current()
    if collector is None:
        return
    collector.emit_debug(
        stage="cache", name="cache_manager.get",
        duration_ms=(_time.monotonic() - start_monotonic) * 1000,
        status=status, dimension="cache",
        payload={"key_hash": hash(str(key)) % 10**8, "tier_hit": tier_hit},
    )


class CacheManager:
    """三级缓存管理器。

    L1 — 进程内 LRUCache（ cachetools ）
    L2 — Redis String（ LZ4 压缩）
    L3 — Qdrant 向量相似度搜索

    容量保护:
    - L1: LRU 自动淘汰 + 大对象过滤（≤ 100KB）
    - L2: Redis maxmemory + TTL + 大对象过滤（≤ 500KB）
    - L3: token_count 阈值 + 定期清理过期向量
    """

    def __init__(
        self,
        l1_maxsize: int = 1000,
        l2_default_ttl: int = 3600,
        l3_default_ttl: int = 86400,
        l1_max_value_bytes: int = L1_MAX_VALUE_BYTES,
        l2_max_value_bytes: int = L2_MAX_VALUE_BYTES,
        l3_min_token_count: int = L3_MIN_TOKEN_COUNT,
    ) -> None:
        """
        Args:
            l1_maxsize: L1 缓存最大条目数，默认 1000（TECH_SPEC.md）。
            l2_default_ttl: L2 Redis 缓存默认 TTL（秒），默认 3600。
            l3_default_ttl: L3 Qdrant 缓存默认 TTL（秒），默认 86400。
            l1_max_value_bytes: L1 单条最大字节数，默认 100KB。
            l2_max_value_bytes: L2 单条最大字节数，默认 500KB。
            l3_min_token_count: L3 回填最小 token 数阈值，默认 100。
        """
        # L1: 进程内 LRUCache
        self._l1: LRUCache[str, str] = LRUCache(maxsize=l1_maxsize)
        self._l1_lock = threading.Lock()

        # L2: Redis TTL（秒）
        self.l2_default_ttl = l2_default_ttl

        # L3: Qdrant TTL（秒）
        self.l3_default_ttl = l3_default_ttl

        # 容量保护配置
        self.l1_max_value_bytes = l1_max_value_bytes
        self.l2_max_value_bytes = l2_max_value_bytes
        self.l3_min_token_count = l3_min_token_count

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
        """写入 L1 缓存，带大对象过滤。

        Args:
            key: 缓存键（SHA-256 哈希）。
            value: 完整 OpenAI 格式响应 JSON 字符串。
            ttl: 生存时间（秒），L1 使用 LRU 淘汰，此参数忽略。
        """
        value_size = len(value.encode("utf-8"))
        if value_size > self.l1_max_value_bytes:
            logger.debug("L1 跳过: value 过大 (%d bytes > %d)", value_size, self.l1_max_value_bytes)
            return
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

        DB_SCHEMA §3: Key 格式 aigateway:cache:v2:{cache_key_hash}
        (v1 前缀已废弃,v2 加入 pipeline_kind / model_family / 参数分桶 /
         cache_scope 分层设计,详见 generate_cache_key 文档)

        Args:
            key: 缓存键(SHA-256 哈希)。

        Returns:
            响应 JSON 字符串,未命中返回 None。
        """
        if self._redis_client is None:
            logger.warning("L2 缓存: Redis 客户端未初始化")
            return None

        redis_key = f"aigateway:cache:v2:{key}"
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
        """写入 L2 Redis 缓存,带大对象过滤。

        DB_SCHEMA §3:
        - Key 格式: aigateway:cache:v2:{cache_key_hash}
          (v1 已废弃,详见 generate_cache_key 文档)
        - TTL 默认 3600 秒,由 prompt_cache.config.ttl 决定
        - 压缩: LZ4

        Args:
            key: 缓存键(SHA-256 哈希)。
            value: 完整 OpenAI 格式响应 JSON 字符串。
            ttl: 生存时间(秒),默认为 l2_default_ttl。
        """
        if self._redis_client is None:
            logger.warning("L2 缓存: Redis 客户端未初始化")
            return

        value_size = len(value.encode("utf-8"))
        if value_size > self.l2_max_value_bytes:
            logger.debug("L2 跳过: value 过大 (%d bytes > %d)", value_size, self.l2_max_value_bytes)
            return

        ttl = ttl or self.l2_default_ttl
        redis_key = f"aigateway:cache:v2:{key}"

        compressed = self._compress(value)

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
            vector: 归一化 prompt 的嵌入向量（1024 维，Qwen3-Embedding-0.6B）。
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
        embedding_model: str = "Qwen/Qwen3-Embedding-0.6B",
        management_mode: str = "auto",
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
            "ttl": now + ttl_seconds if management_mode == "auto" else 0,
            "hit_count": 0,
            "token_count": token_count,
            "cache_tier": "L3",
            "embedding_model": embedding_model,
            "management_mode": management_mode,
        }

        await self._qdrant_client.store_embedding(
            collection="semantic_cache",
            payload=payload,
            vector=vector,
        )
        logger.debug("L3 缓存写入: prompt_hash=%s model=%s mode=%s", prompt_hash[:16], model, management_mode)

    # ------------------------------------------------------------------
    # 回填逻辑（Backfill）
    # ------------------------------------------------------------------

    async def backfill_on_l2_hit(self, key: str, response_json: str) -> None:
        """L2 命中时回填 L1。"""
        self.l1_set(key, response_json)

    async def backfill_on_l3_hit(self, key: str, response_json: str) -> None:
        """L3 命中时仅回填 L1，不回填 L2。

        原因：L3 是语义近似匹配，其响应的 cache_key 与当前请求的精确 key 不同。
        将近似响应写入 L2 会导致后续精确匹配时返回错误结果。
        """
        self.l1_set(key, response_json)

    async def backfill_on_miss(
        self,
        key: str,
        response_json: str,
        normalized_prompt: str,
        model: str,
        user_id: str,
        token_count: int,
        compute_embedding_fn: Optional[Callable[[str], Awaitable[List[float]]]] = None,
    ) -> None:
        """全部未命中时回填：L1 + L2 同步，L3 有条件异步。

        Args:
            key: 缓存键。
            response_json: LLM 响应 JSON 字符串。
            normalized_prompt: 归一化后的 prompt。
            model: 模型名称。
            user_id: 用户 ID。
            token_count: 请求 token 数。
            compute_embedding_fn: 计算 embedding 的异步函数。
        """
        # L1 回填（带大对象过滤）
        self.l1_set(key, response_json)
        # L2 回填（带大对象过滤 + TTL）
        await self.l2_set(key, response_json)

        # L3 回填：仅对 token 消耗较高的请求执行（节省 embedding 计算）
        if compute_embedding_fn is not None and token_count >= self.l3_min_token_count:
            asyncio.create_task(
                self._safe_l3_backfill(
                    key, response_json, normalized_prompt,
                    model, user_id, token_count, compute_embedding_fn,
                )
            )
        else:
            if token_count < self.l3_min_token_count:
                logger.debug(
                    "L3 跳过回填: token_count=%d < 阈值 %d",
                    token_count, self.l3_min_token_count,
                )

    async def _safe_l3_backfill(
        self,
        key: str,
        response_json: str,
        normalized_prompt: str,
        model: str,
        user_id: str,
        token_count: int,
        compute_embedding_fn: Callable[[str], Awaitable[List[float]]],
    ) -> None:
        """L3 异步回填，失败不影响主流程。"""
        try:
            vector = await compute_embedding_fn(normalized_prompt)
            await self.l3_store(
                prompt_hash=key,
                prompt_normalized=normalized_prompt,
                model=model,
                response_json=response_json,
                user_id=user_id,
                token_count=token_count,
                vector=vector,
            )
        except Exception as exc:
            logger.warning("L3 backfill failed: %s", exc)

    # ------------------------------------------------------------------
    # L3 容量保护 — 过期清理
    # ------------------------------------------------------------------

    async def cleanup_expired_l3(self) -> int:
        """定期清理 Qdrant 中已过期的缓存向量（仅清理 mode=auto 且已过期的条目）。

        Returns:
            删除的条目数量。
        """
        if self._qdrant_client is None:
            return 0
        now = int(time.time())
        try:
            deleted = await self._qdrant_client.delete_by_filter(
                collection="semantic_cache",
                filter={
                    "must": [
                        {"key": "management_mode", "match": {"value": "auto"}},
                        {"key": "ttl", "range": {"lt": now, "gt": 0}},
                    ]
                },
            )
            logger.info("L3 自动清理完成: 删除 %d 条过期条目", deleted)
            return deleted
        except Exception as exc:
            logger.warning("L3 清理失败: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # 缓存键生成
    # ------------------------------------------------------------------

    @staticmethod
    def generate_cache_key(
        normalized_prompt: str,
        model: str,
        pipeline_kind: str = "understanding",
        cache_scope: str = "shared",
        user_id: str = "",
        tenant_id: str = "",
        **params: Any,
    ) -> str:
        """生成缓存键 v2(SHA-256)。

        v2 分层设计(vs v1):
        - normalized_prompt: 调用方传入前已通过 `_normalize_prompt` 归一化;
          且推荐只包含 "system + 最后 N 轮对话",而非整个 messages 数组
          (让多轮对话末尾一致的请求也能命中,由 dispatcher 负责裁剪)。
        - model: 内部转 model_family(去掉尾部日期 snapshot)。用户如需强制
          精确匹配某个 snapshot,应在 dispatcher 层判断并把完整 model_id
          原样传入,不走此函数默认路径。
        - temperature/max_tokens: 分桶合并 SDK 默认值细微偏差。
        - top_p: 忽略(实践中几乎全是 1.0,即使不是也归 family+temp 桶已够)。
        - pipeline_kind: understanding / generation 强制隔离,防止跨管道
          结果污染(生成管道图片描述被理解管道文本命中就是灾难)。
        - cache_scope=shared(默认): 不带 user_id,同租户共享(命中率主要
          提升来源)。scope=private: 带 user_id,严格隔离(比如带 PII 的
          请求)。
        - tenant_id: 组织级隔离,与 user_id 独立。

        Args:
            normalized_prompt: 归一化后的 prompt 文本(建议 dispatcher 已用
                `_normalize_prompt(system + tail N 轮)` 处理过)。
            model: 模型名称,内部转 family。
            pipeline_kind: "understanding" | "generation",默认 understanding。
            cache_scope: "shared" | "private",默认 shared。
            user_id: 用户 ID,仅 scope=private 时纳入 key。
            tenant_id: 租户/组织 ID,默认空字符串(单租户部署)。
            **params: 支持 temperature / max_tokens / top_p 三个采样参数;
                其他 kwargs 会按 key 排序纳入(向前兼容测试)。

        Returns:
            64 位 hex SHA-256 哈希字符串。前缀由 l2_set/l2_get 拼
            `aigateway:cache:v2:`。
        """
        # 采样参数分桶
        temperature = params.pop("temperature", None)
        max_tokens = params.pop("max_tokens", None)
        # top_p 明确忽略(实践命中率贡献极小,反而拉高 MISS 率)
        params.pop("top_p", None)

        temp_bucket = _bucket_temperature(temperature)
        mt_bucket = _bucket_max_tokens(max_tokens)

        # model → family(auto 特殊值保留原样)
        family = "auto" if model == "auto" else _model_family(model)

        # 组装 key 段:固定顺序,避免同参数不同顺序 hash 不同
        parts: List[str] = [
            "v2",  # schema 版本,方便未来 v3 平滑升级
            (tenant_id or ""),
            pipeline_kind or "understanding",
            family,
            temp_bucket,
            mt_bucket,
        ]
        # scope=private 时才拼 user_id;shared 时不拼,同租户共享
        if cache_scope == "private" and user_id:
            parts.append(f"u={user_id}")
        # 未来扩展保留:其他额外 kwargs 按 key 排序纳入(兼容旧测试)
        for k in sorted(params.keys()):
            v = params[k]
            if v is not None:
                parts.append(f"{k}={v}")
        # normalized_prompt 放最后(通常最长)
        parts.append(normalized_prompt or "")

        key_string = "|".join(parts)
        return hashlib.sha256(key_string.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # 多级缓存联动
    # ------------------------------------------------------------------

    async def get(self, key: str, value_fn=None, **params: Any) -> Optional[Dict[str, Any]]:
        """多级缓存穿透查询。

        依次检查 L1 -> L2 -> L3，命中时执行对应回填策略。

        回填策略:
        - L2 命中 → 回填 L1
        - L3 命中 → 仅回填 L1（不回填 L2）
        - MISS → 回填 L1 + L2 + 有条件回填 L3

        Args:
            key: 缓存键。
            value_fn: 未命中时的回调函数，返回 (value_str, extra_meta)。
            **params: 传递给 value_fn 的额外参数。

        Returns:
            缓存结果字典 {hit_tier, value, meta} 或 None。
        """
        import time as _time
        _start = _time.monotonic()
        # L1 查询
        cached = self.l1_get(key)
        if cached is not None:
            _emit_cache_debug(key, "L1", _start, "ok")
            return {"hit_tier": "L1", "value": cached, "meta": params}

        # L2 查询
        l2_value = await self.l2_get(key)
        if l2_value is not None:
            # L2 命中 → 回填 L1
            await self.backfill_on_l2_hit(key, l2_value)
            _emit_cache_debug(key, "L2", _start, "ok")
            return {"hit_tier": "L2", "value": l2_value, "meta": params}

        # L3 查询（语义缓存，需要先计算向量）
        if "vector" in params:
            l3_result = await self.l3_query(
                vector=params["vector"],
                threshold=params.get("threshold", 0.95),
                user_id=params.get("user_id"),
            )
            if l3_result is not None:
                response_json = l3_result.get("response_json", "")
                if response_json:
                    # L3 命中 → 仅回填 L1（不回填 L2）
                    await self.backfill_on_l3_hit(key, response_json)
                    _emit_cache_debug(key, "L3", _start, "ok")
                    return {"hit_tier": "L3", "value": response_json, "meta": l3_result}

        # 三级均未命中，调用 value_fn 获取数据
        if callable(value_fn):
            result = value_fn(**params)
            if result is not None:
                value_str, extra = result if isinstance(result, tuple) else (result, {})
                # 使用新的回填逻辑
                token_count = extra.get("token_count", 0)
                compute_embedding_fn = extra.get("compute_embedding_fn")

                await self.backfill_on_miss(
                    key=key,
                    response_json=value_str,
                    normalized_prompt=params.get("normalized_prompt", ""),
                    model=params.get("model", ""),
                    user_id=params.get("user_id", ""),
                    token_count=token_count,
                    compute_embedding_fn=compute_embedding_fn,
                )
                _emit_cache_debug(key, "MISS", _start, "ok")
                return {"hit_tier": "MISS", "value": value_str, "meta": extra}

        _emit_cache_debug(key, "none", _start, "skip")
        return None



# ------------------------------------------------------------------
# L3 定时清理调度器
# ------------------------------------------------------------------


class L3CleanupScheduler:
    """L3 缓存定时清理调度器。

    按配置的间隔定期清理 mode=auto 且已过期的 L3 缓存条目。
    """

    def __init__(self, cache_manager: CacheManager, interval_minutes: int = 60):
        self._cache_manager = cache_manager
        self._interval_minutes = interval_minutes
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """启动定时清理任务。"""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._cleanup_loop())
        logger.info("L3 清理调度器已启动: 间隔 %d 分钟", self._interval_minutes)

    async def stop(self) -> None:
        """停止定时清理任务。"""
        if self._task:
            self._task.cancel()
            self._task = None
            logger.info("L3 清理调度器已停止")

    def update_interval(self, interval_minutes: int) -> None:
        """动态更新清理间隔（前端配置变更时调用）。"""
        self._interval_minutes = interval_minutes
        logger.info("L3 清理间隔已更新: %d 分钟", interval_minutes)

    async def _cleanup_loop(self) -> None:
        """清理循环：每隔 interval_minutes 执行一次清理。"""
        while True:
            try:
                await asyncio.sleep(self._interval_minutes * 60)
                await self._cache_manager.cleanup_expired_l3()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("L3 清理任务异常: %s", exc)


# ------------------------------------------------------------------
# Reranker 接口和实现
# ------------------------------------------------------------------


class LightweightReranker:
    """轻量 reranker：基于关键词重叠度的启发式打分。

    优点: 零依赖，零延迟，不需要模型
    缺点: 精度不如 cross-encoder
    适用: 不想加载额外模型时的降级方案
    """

    async def rerank(self, query: str, documents: List[str]) -> List[float]:
        """对文档列表相对于 query 进行打分。"""
        scores = []
        query_tokens = set(query.lower().split())
        for doc in documents:
            doc_tokens = set(doc.lower().split())
            overlap = len(query_tokens & doc_tokens)
            score = overlap / max(len(query_tokens), 1)
            scores.append(score)
        return scores


class CrossEncoderReranker:
    """基于 sentence-transformers CrossEncoder 的本地 reranker。

    模型: cross-encoder/ms-marco-MiniLM-L-6-v2（~80MB, 推理 <10ms/对）
    优点: 本地推理，无 API 成本，延迟可控
    缺点: 需要额外 ~80MB 内存
    """

    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        self._model_name = model_name
        self._model: Any = None

    def _ensure_model(self) -> None:
        """懒加载模型。"""
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder
                self._model = CrossEncoder(self._model_name)
            except ImportError:
                logger.warning("sentence-transformers 未安装，CrossEncoderReranker 不可用")
                raise

    async def rerank(self, query: str, documents: List[str]) -> List[float]:
        """Cross-encoder 打分（在线程池中执行避免阻塞）。"""
        self._ensure_model()
        loop = asyncio.get_event_loop()
        pairs = [(query, doc) for doc in documents]
        scores = await loop.run_in_executor(None, self._model.predict, pairs)
        return scores.tolist()


class SemanticCacheWithRerank:
    """带 rerank 的语义缓存查询。

    使用 Retrieve + Rerank 两阶段策略：
    1. Qdrant 粗检索 top-K（放宽阈值召回更多候选）
    2. Reranker 精排，取最佳候选判断是否超过阈值
    """

    def __init__(
        self,
        cache_manager: CacheManager,
        reranker: Any = None,
        retrieve_top_k: int = 5,
        retrieve_threshold: float = 0.90,
        rerank_threshold: float = 0.85,
    ):
        self._cache_manager = cache_manager
        self._reranker = reranker
        self._retrieve_top_k = retrieve_top_k
        self._retrieve_threshold = retrieve_threshold
        self._rerank_threshold = rerank_threshold

    async def query_with_rerank(
        self,
        query_text: str,
        vector: List[float],
        user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve + Rerank 两阶段语义缓存查询。

        Args:
            query_text: 原始查询文本（用于 rerank）。
            vector: 查询向量。
            user_id: 多租户隔离。

        Returns:
            命中结果字典 或 None。
        """
        qdrant = self._cache_manager._qdrant_client
        if qdrant is None:
            return None

        # Stage 1: 粗检索 — Qdrant 向量召回 top-K
        candidates = await qdrant.query_vector_multi(
            collection="semantic_cache",
            vector=vector,
            limit=self._retrieve_top_k,
            score_threshold=self._retrieve_threshold,
            user_id=user_id,
        )

        if not candidates:
            return None

        # Stage 2: 精排
        if self._reranker and len(candidates) > 1:
            candidate_texts = [
                c.get("payload", {}).get("prompt_normalized", "") for c in candidates
            ]
            rerank_scores = await self._reranker.rerank(
                query=query_text,
                documents=candidate_texts,
            )
            # 按 rerank score 排序
            scored = sorted(
                zip(candidates, rerank_scores),
                key=lambda x: x[1],
                reverse=True,
            )
            best_candidate, best_score = scored[0]
        else:
            # 无 reranker 或只有一个候选，直接用向量相似度
            best_candidate = candidates[0]
            best_score = best_candidate.get("score", 0)

        # 阈值判断
        if best_score < self._rerank_threshold:
            logger.debug(
                "Rerank 未达标: best_score=%.4f < threshold=%.4f",
                best_score, self._rerank_threshold,
            )
            return None

        payload = best_candidate.get("payload", {})

        # 检查 TTL 过期
        ttl_expire = payload.get("ttl", 0)
        now = int(time.time())
        if ttl_expire > 0 and now > ttl_expire:
            return None

        return {
            "response_json": payload.get("response_json", ""),
            "score": best_score,
            "hit_count": payload.get("hit_count", 0),
            "reranked": len(candidates) > 1 and self._reranker is not None,
        }
