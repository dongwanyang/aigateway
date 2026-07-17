"""L1/L2/L3 cache orchestration — CacheManager, scheduler, rerankers.

Moved from ``aigateway_core.caching`` as part of the 总分总 runtime split
(Task 3). The cache-key helpers live in ``aigateway_core.prefix.cache.cache_keys``.

Three-tier cache:
- L1: in-process LRUCache (cachetools)
- L2: Redis String (LZ4 compression)
- L3: Qdrant vector similarity search

Backfill strategy:
- L2 hit → backfill L1
- L3 hit → backfill L1 only (L3 is semantic approximate match)
- MISS → backfill L1 + L2 + conditionally L3
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import lz4.frame
import threading
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from cachetools import LRUCache

from .cache_keys import _bucket_max_tokens, _bucket_temperature, _model_family

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Capacity protection constants
# ------------------------------------------------------------------

L1_MAX_VALUE_BYTES = 102400    # single entry max 100KB, skip L1 if exceeded
L2_MAX_VALUE_BYTES = 512000    # single entry max 500KB, skip L2 if exceeded
L3_MIN_TOKEN_COUNT = 100       # only write L3 for requests with token_count > 100
L3_DEFAULT_TTL = 86400         # 24 hours expiry
L3_CLEANUP_INTERVAL = 3600     # clean expired vectors every hour


def _emit_cache_debug(key: str, tier_hit: str, start_monotonic: float,
                      status: str = "ok") -> None:
    """CacheManager.get 的 stage 事件已由 dispatcher 在 cache lookup 路径发出,
    不需要额外发 kind=debug 事件 —— 否则 trace 里同一操作出现两行(stage+debug)。
    保留此 stub 以便后续需要时通过 stage 事件的 payload 字段查看缓存信息。
    """


class CacheManager:
    """Three-tier cache manager.

    L1 — in-process LRUCache (cachetools)
    L2 — Redis String (LZ4 compression)
    L3 — Qdrant vector similarity search

    Capacity protection:
    - L1: LRU auto-eviction + large object filter (≤ 100KB)
    - L2: Redis maxmemory + TTL + large object filter (≤ 500KB)
    - L3: token_count threshold + periodic expired-vector cleanup
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
            l1_maxsize: L1 cache max entries, default 1000 (TECH_SPEC.md).
            l2_default_ttl: L2 Redis cache default TTL (seconds), default 3600.
            l3_default_ttl: L3 Qdrant cache default TTL (seconds), default 86400.
            l1_max_value_bytes: L1 single entry max bytes, default 100KB.
            l2_max_value_bytes: L2 single entry max bytes, default 500KB.
            l3_min_token_count: L3 backfill min token threshold, default 100.
        """
        # L1: in-process LRUCache
        self._l1: LRUCache[str, str] = LRUCache(maxsize=l1_maxsize)
        self._l1_lock = threading.Lock()

        # L2: Redis TTL (seconds)
        self.l2_default_ttl = l2_default_ttl

        # L3: Qdrant TTL (seconds)
        self.l3_default_ttl = l3_default_ttl

        # Capacity protection config
        self.l1_max_value_bytes = l1_max_value_bytes
        self.l2_max_value_bytes = l2_max_value_bytes
        self.l3_min_token_count = l3_min_token_count

        # L2 and L3 clients (externally injected, to avoid circular deps)
        self._redis_client: Any = None  # RedisClientManager
        self._qdrant_client: Any = None  # QdrantClientManager

    def set_redis_client(self, client: Any) -> None:
        """Inject Redis client instance (decouple dependency)."""
        self._redis_client = client

    def set_qdrant_client(self, client: Any) -> None:
        """Inject Qdrant client instance (decouple dependency)."""
        self._qdrant_client = client

    # ------------------------------------------------------------------
    # L1: in-process cache (cachetools.LRUCache)
    # ------------------------------------------------------------------

    def l1_get(self, key: str) -> Optional[str]:
        """Read from L1 cache.

        DB_SCHEMA §3: Key is SHA-256(normalized_prompt + model + params)

        Args:
            key: cache key (SHA-256 hash string).

        Returns:
            Cached value (JSON string), None if miss.
        """
        with self._l1_lock:
            value = self._l1.get(key)
            if value is not None:
                logger.debug("L1 缓存命中: key=%s...", key[:16])
            return value

    def l1_set(self, key: str, value: str, ttl: Optional[int] = None) -> None:
        """Write to L1 cache, with large-object filter.

        Args:
            key: cache key (SHA-256 hash).
            value: full OpenAI-format response JSON string.
            ttl: time-to-live (seconds); L1 uses LRU eviction, this param ignored.
        """
        value_size = len(value.encode("utf-8"))
        if value_size > self.l1_max_value_bytes:
            logger.debug("L1 跳过: value 过大 (%d bytes > %d)", value_size, self.l1_max_value_bytes)
            return
        with self._l1_lock:
            self._l1[key] = value
            logger.debug("L1 缓存写入: key=%s", key[:16])

    # ------------------------------------------------------------------
    # L2: Redis KV cache (LZ4 compression)
    # ------------------------------------------------------------------

    def _compress(self, data: str) -> bytes:
        """Compress data with LZ4.

        DB_SCHEMA §3: L2 cache uses LZ4 compression
        """
        return lz4.frame.compress(
            data.encode("utf-8"),
            compression_level=9,
            store_size=False,
        )

    def _decompress(self, data: bytes) -> str:
        """Decompress LZ4-compressed data."""
        return lz4.frame.decompress(data).decode("utf-8")

    async def l2_get(self, key: str) -> Optional[str]:
        """Read from L2 Redis cache.

        DB_SCHEMA §3: Key format aigateway:cache:v2:{cache_key_hash}
        (v1 prefix deprecated; v2 adds pipeline_kind / model_family /
         parameter bucketing / cache_scope layering — see generate_cache_key docs)

        Args:
            key: cache key (SHA-256 hash).

        Returns:
            Response JSON string, None if miss.
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
        """Write to L2 Redis cache, with large-object filter.

        DB_SCHEMA §3:
        - Key format: aigateway:cache:v2:{cache_key_hash}
          (v1 deprecated, see generate_cache_key docs)
        - TTL default 3600 seconds, set by prompt_cache.config.ttl
        - Compression: LZ4

        Args:
            key: cache key (SHA-256 hash).
            value: full OpenAI-format response JSON string.
            ttl: time-to-live (seconds), defaults to l2_default_ttl.
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
    # L3: Qdrant vector cache
    # ------------------------------------------------------------------

    async def l3_query(
        self,
        vector: List[float],
        threshold: float = 0.95,
        limit: int = 1,
        user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Qdrant vector similarity search (L3 semantic cache).

        DB_SCHEMA §Qdrant semantic cache collection query params:
        - limit default 1
        - score_threshold default 0.95

        Args:
            vector: normalized prompt embedding vector (1024-dim, Qwen3-Embedding-0.6B).
            threshold: similarity threshold, default 0.95.
            limit: max results, default 1.
            user_id: multi-tenant isolation user ID.

        Returns:
            Match result dict {id, score, payload}, None if miss.
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

        # Extract cached response from payload
        payload = result.get("payload", {})
        response_json = payload.get("response_json", "")

        # Check TTL expiry (defensive)
        ttl_expire = payload.get("ttl", 0)
        now = int(time.time())
        if ttl_expire > 0 and now > ttl_expire:
            logger.debug("L3 缓存结果已过期: ttl_expire=%d now=%d", ttl_expire, now)
            return None

        # Increment hit count (best-effort; race condition in multi-instance
        # deployments is acceptable — lost increments don't affect correctness)
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
        """Store cache result to L3 Qdrant.

        DB_SCHEMA §Qdrant semantic cache collection Payload Schema
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
    # Backfill logic
    # ------------------------------------------------------------------

    async def backfill_on_l2_hit(self, key: str, response_json: str) -> None:
        """L2 hit → backfill L1."""
        self.l1_set(key, response_json)

    async def backfill_on_l3_hit(self, key: str, response_json: str) -> None:
        """L3 hit → backfill L1 only, not L2.

        Reason: L3 is semantic approximate match; its response cache_key
        differs from the current request's exact key. Writing the
        approximate response to L2 would cause wrong results on later
        exact matches.
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
        """All-miss backfill: L1 + L2 sync, L3 conditional async.

        Args:
            key: cache key.
            response_json: LLM response JSON string.
            normalized_prompt: normalized prompt.
            model: model name.
            user_id: user ID.
            token_count: request token count.
            compute_embedding_fn: async function to compute embedding.
        """
        # L1 backfill (with large-object filter)
        self.l1_set(key, response_json)
        # L2 backfill (with large-object filter + TTL)
        await self.l2_set(key, response_json)

        # L3 backfill: only for high-token requests (save embedding compute)
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
        """L3 async backfill; failure doesn't affect main flow."""
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
    # L3 capacity protection — expired cleanup
    # ------------------------------------------------------------------

    async def cleanup_expired_l3(self) -> int:
        """Periodically clean expired cache vectors from Qdrant (mode=auto, expired only).

        Returns:
            Number of deleted entries.
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
    # Cache key generation
    # ------------------------------------------------------------------

    @staticmethod
    def generate_cache_key(
        normalized_prompt: str,
        model: str,
        pipeline_kind: str = "understanding",
        cache_scope: str = "group",
        user_id: str = "",
        group_id: str = "",
        **params: Any,
    ) -> str:
        """Generate cache key v2 (SHA-256).

        v2 layered design (vs v1):
        - normalized_prompt: caller pre-normalizes via `_normalize_prompt`;
          recommended to include only "system + last N turns" rather than the
          full messages array (so multi-turn chats with identical tails still
          hit, dispatcher handles trimming).
        - model: internally converts to model_family (strips trailing date
          snapshot). To force exact match on a specific snapshot, judge at
          dispatcher layer and pass the full model_id as-is, bypassing this
          function's default path.
        - temperature/max_tokens: bucketed to merge minor SDK default diffs.
        - top_p: ignored (almost always 1.0 in practice; even if not, the
          family+temp bucket is sufficient).
        - pipeline_kind: understanding / generation strictly isolated, preventing
          cross-pipeline result contamination (generation image description
          hit by understanding text = disaster).
        - cache_scope=group (default): includes group_id, shared among group
          members. scope=private: includes user_id, strict per-user isolation
          (e.g. PII-bearing requests). scope=public: no user/group id, shared
          globally.
        - tenant_id: removed (unused — group_id replaces it for multi-tenant
          isolation).

        Args:
            normalized_prompt: normalized prompt text (dispatcher should have
                run `_normalize_prompt(system + tail N turns)`).
            model: model name, internally converted to family.
            pipeline_kind: "understanding" | "generation", default understanding.
            cache_scope: "private" | "group" | "public", default "group".
            user_id: user ID, only included when scope=private.
            group_id: group ID, only included when scope=group.
            **params: supports temperature / max_tokens / top_p sampling params;
                other kwargs included sorted by key (forward-compatible tests).

        Returns:
            64-hex-char SHA-256 hash string. Prefix is prepended by l2_set/l2_get
            as `aigateway:cache:v2:`.
        """
        # Bucket sampling params
        temperature = params.pop("temperature", None)
        max_tokens = params.pop("max_tokens", None)
        # top_p explicitly ignored (marginal hit-rate contribution, raises MISS rate)
        params.pop("top_p", None)

        temp_bucket = _bucket_temperature(temperature)
        mt_bucket = _bucket_max_tokens(max_tokens)

        # model → family (auto special value kept as-is)
        family = "auto" if model == "auto" else _model_family(model)

        # Assemble key segments: fixed order to avoid same-params-different-order hash
        parts: List[str] = [
            "v2",  # schema version, for smooth future v3 upgrade
            pipeline_kind or "understanding",
            family,
            temp_bucket,
            mt_bucket,
        ]
        # Scope-specific identifier
        if cache_scope == "private" and user_id:
            parts.append(f"u={user_id}")
        elif cache_scope == "group" and group_id:
            parts.append(f"g={group_id}")
        # public scope: no user/group id appended — shared globally
        # Future extension: other extra kwargs included sorted by key (compat with old tests)
        for k in sorted(params.keys()):
            v = params[k]
            if v is not None:
                parts.append(f"{k}={v}")
        # normalized_prompt last (usually longest)
        parts.append(normalized_prompt or "")

        key_string = "|".join(parts)
        return hashlib.sha256(key_string.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Multi-tier cache orchestration
    # ------------------------------------------------------------------

    async def get(self, key: str, value_fn=None, **params: Any) -> Optional[Dict[str, Any]]:
        """Multi-tier cache lookup.

        Checks L1 -> L2 -> L3 in order; on hit executes the corresponding
        backfill strategy.

        Backfill strategy:
        - L2 hit → backfill L1
        - L3 hit → backfill L1 only (not L2)
        - MISS → backfill L1 + L2 + conditionally L3

        Args:
            key: cache key.
            value_fn: callback on miss, returns (value_str, extra_meta).
            **params: extra params passed to value_fn.

        Returns:
            Cache result dict {hit_tier, value, meta} or None.
        """
        import time as _time
        _start = _time.monotonic()
        # L1 query
        cached = self.l1_get(key)
        if cached is not None:
            _emit_cache_debug(key, "L1", _start, "ok")
            return {"hit_tier": "L1", "value": cached, "meta": params}

        # L2 query
        l2_value = await self.l2_get(key)
        if l2_value is not None:
            # L2 hit → backfill L1
            await self.backfill_on_l2_hit(key, l2_value)
            _emit_cache_debug(key, "L2", _start, "ok")
            return {"hit_tier": "L2", "value": l2_value, "meta": params}

        # L3 query (semantic cache, needs vector computed first)
        if "vector" in params:
            l3_result = await self.l3_query(
                vector=params["vector"],
                threshold=params.get("threshold", 0.95),
                user_id=params.get("user_id"),
            )
            if l3_result is not None:
                response_json = l3_result.get("response_json", "")
                if response_json:
                    # L3 hit → backfill L1 only (not L2)
                    await self.backfill_on_l3_hit(key, response_json)
                    _emit_cache_debug(key, "L3", _start, "ok")
                    return {"hit_tier": "L3", "value": response_json, "meta": l3_result}

        # All three tiers missed, call value_fn to fetch data
        if callable(value_fn):
            result = value_fn(**params)
            if result is not None:
                value_str, extra = result if isinstance(result, tuple) else (result, {})
                # Use the new backfill logic
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
# L3 scheduled cleanup scheduler
# ------------------------------------------------------------------


class L3CleanupScheduler:
    """L3 cache periodic cleanup scheduler.

    Periodically cleans expired mode=auto L3 cache entries at a configured
    interval.
    """

    def __init__(self, cache_manager: CacheManager, interval_minutes: int = 60):
        self._cache_manager = cache_manager
        self._interval_minutes = interval_minutes
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the periodic cleanup task."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._cleanup_loop())
        logger.info("L3 清理调度器已启动: 间隔 %d 分钟", self._interval_minutes)

    async def stop(self) -> None:
        """Stop the periodic cleanup task."""
        if self._task:
            self._task.cancel()
            self._task = None
            logger.info("L3 清理调度器已停止")

    def update_interval(self, interval_minutes: int) -> None:
        """Dynamically update cleanup interval (called on frontend config change)."""
        self._interval_minutes = interval_minutes
        logger.info("L3 清理间隔已更新: %d 分钟", interval_minutes)

    async def _cleanup_loop(self) -> None:
        """Cleanup loop: run cleanup every interval_minutes."""
        while True:
            try:
                await asyncio.sleep(self._interval_minutes * 60)
                await self._cache_manager.cleanup_expired_l3()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("L3 清理任务异常: %s", exc)


# ------------------------------------------------------------------
# Reranker interface and implementations
# ------------------------------------------------------------------


class LightweightReranker:
    """Lightweight reranker: keyword-overlap heuristic scoring.

    Pros: zero deps, zero latency, no model needed
    Cons: lower precision than cross-encoder
    Use: fallback when no extra model load desired
    """

    async def rerank(self, query: str, documents: List[str]) -> List[float]:
        """Score documents relative to query."""
        scores = []
        query_tokens = set(query.lower().split())
        for doc in documents:
            doc_tokens = set(doc.lower().split())
            overlap = len(query_tokens & doc_tokens)
            score = overlap / max(len(query_tokens), 1)
            scores.append(score)
        return scores


class CrossEncoderReranker:
    """Local reranker based on sentence-transformers CrossEncoder.

    Model: cross-encoder/ms-marco-MiniLM-L-6-v2 (~80MB, inference <10ms/pair)
    Pros: local inference, no API cost, controllable latency
    Cons: needs ~80MB extra memory
    """

    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        self._model_name = model_name
        self._model: Any = None

    def _ensure_model(self) -> None:
        """Lazy-load model."""
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder
                self._model = CrossEncoder(self._model_name)
            except ImportError:
                logger.warning("sentence-transformers 未安装，CrossEncoderReranker 不可用")
                raise

    async def rerank(self, query: str, documents: List[str]) -> List[float]:
        """Cross-encoder scoring (run in thread pool to avoid blocking)."""
        self._ensure_model()
        loop = asyncio.get_event_loop()
        pairs = [(query, doc) for doc in documents]
        scores = await loop.run_in_executor(None, self._model.predict, pairs)
        return scores.tolist()


class SemanticCacheWithRerank:
    """Semantic cache query with rerank.

    Uses a Retrieve + Rerank two-stage strategy:
    1. Qdrant coarse retrieve top-K (relaxed threshold to recall more candidates)
    2. Reranker fine-rank, take best candidate and judge if it exceeds threshold
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
        """Retrieve + Rerank two-stage semantic cache query.

        Args:
            query_text: original query text (for rerank).
            vector: query vector.
            user_id: multi-tenant isolation.

        Returns:
            Hit result dict or None.
        """
        qdrant = self._cache_manager._qdrant_client
        if qdrant is None:
            return None

        # Stage 1: coarse retrieve — Qdrant vector recall top-K
        candidates = await qdrant.query_vector_multi(
            collection="semantic_cache",
            vector=vector,
            limit=self._retrieve_top_k,
            score_threshold=self._retrieve_threshold,
            user_id=user_id,
        )

        if not candidates:
            return None

        # Stage 2: fine-rank
        if self._reranker and len(candidates) > 1:
            candidate_texts = [
                c.get("payload", {}).get("prompt_normalized", "") for c in candidates
            ]
            rerank_scores = await self._reranker.rerank(
                query=query_text,
                documents=candidate_texts,
            )
            # Sort by rerank score
            scored = sorted(
                zip(candidates, rerank_scores),
                key=lambda x: x[1],
                reverse=True,
            )
            best_candidate, best_score = scored[0]
        else:
            # No reranker or only one candidate, use vector similarity directly
            best_candidate = candidates[0]
            best_score = best_candidate.get("score", 0)

        # Threshold check
        if best_score < self._rerank_threshold:
            logger.debug(
                "Rerank 未达标: best_score=%.4f < threshold=%.4f",
                best_score, self._rerank_threshold,
            )
            return None

        payload = best_candidate.get("payload", {})

        # Check TTL expiry
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


__all__ = [
    "CacheManager",
    "L3CleanupScheduler",
    "LightweightReranker",
    "CrossEncoderReranker",
    "SemanticCacheWithRerank",
    "_emit_cache_debug",
    "L1_MAX_VALUE_BYTES",
    "L2_MAX_VALUE_BYTES",
    "L3_MIN_TOKEN_COUNT",
    "L3_DEFAULT_TTL",
    "L3_CLEANUP_INTERVAL",
]
