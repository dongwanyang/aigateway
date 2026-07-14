"""CacheManager 行为单元测试.

覆盖:
- L1 get/set + 容量保护 (large object filter)
- L2 get/set + LZ4 压缩/解压
- L3 query/store (mocked qdrant)
- backfill 策略: L2 hit→L1, L3 hit→L1 only, MISS→L1+L2+L3
- capacity constants
- generate_cache_key 集成
- L3CleanupScheduler start/stop/update_interval
- LightweightReranker
- SemanticCacheWithRerank
"""

import asyncio
import sys
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.prefix.cache.cache_manager import (
    CacheManager,
    L3CleanupScheduler,
    LightweightReranker,
    SemanticCacheWithRerank,
    _emit_cache_debug,
    L1_MAX_VALUE_BYTES,
    L2_MAX_VALUE_BYTES,
    L3_MIN_TOKEN_COUNT,
    L3_DEFAULT_TTL,
    L3_CLEANUP_INTERVAL,
)


# ==================================================================
# 常量测试
# ==================================================================


class TestConstants:
    def test_l1_max_value_bytes(self):
        assert L1_MAX_VALUE_BYTES == 102400

    def test_l2_max_value_bytes(self):
        assert L2_MAX_VALUE_BYTES == 512000

    def test_l3_min_token_count(self):
        assert L3_MIN_TOKEN_COUNT == 100

    def test_l3_default_ttl(self):
        assert L3_DEFAULT_TTL == 86400

    def test_l3_cleanup_interval(self):
        assert L3_CLEANUP_INTERVAL == 3600


# ==================================================================
# CacheManager 构造测试
# ==================================================================


class TestCacheManagerInit:
    def test_default_l1_size(self):
        cm = CacheManager()
        assert cm._l1.maxsize == 1000

    def test_custom_l1_maxsize(self):
        cm = CacheManager(l1_maxsize=500)
        assert cm._l1.maxsize == 500

    def test_default_l2_ttl(self):
        cm = CacheManager()
        assert cm.l2_default_ttl == 3600

    def test_default_l3_ttl(self):
        cm = CacheManager()
        assert cm.l3_default_ttl == 86400

    def test_clients_injected_later(self):
        cm = CacheManager()
        assert cm._redis_client is None
        assert cm._qdrant_client is None

    def test_set_redis_client(self):
        cm = CacheManager()
        fake_redis = MagicMock()
        cm.set_redis_client(fake_redis)
        assert cm._redis_client is fake_redis

    def test_set_qdrant_client(self):
        cm = CacheManager()
        fake_qdrant = MagicMock()
        cm.set_qdrant_client(fake_qdrant)
        assert cm._qdrant_client is fake_qdrant


# ==================================================================
# L1 缓存测试
# ==================================================================


class TestL1Cache:
    def setup_method(self):
        self.cm = CacheManager(l1_maxsize=10)

    def test_l1_set_and_get(self):
        self.cm.l1_set("key1", "value1")
        assert self.cm.l1_get("key1") == "value1"

    def test_l1_miss_returns_none(self):
        assert self.cm.l1_get("nonexistent") is None

    def test_l1_large_object_skipped(self):
        big = "x" * (L1_MAX_VALUE_BYTES + 1)
        self.cm.l1_set("big", big)
        assert self.cm.l1_get("big") is None

    def test_l1_exact_boundary(self):
        exact = "x" * L1_MAX_VALUE_BYTES
        self.cm.l1_set("exact", exact)
        assert self.cm.l1_get("exact") == exact

    def test_l1_overwrites(self):
        self.cm.l1_set("k", "v1")
        self.cm.l1_set("k", "v2")
        assert self.cm.l1_get("k") == "v2"

    def test_l1_eviction(self):
        """超过 maxsize 时旧条目应被驱逐。"""
        cm = CacheManager(l1_maxsize=10)
        for i in range(15):
            cm.l1_set(f"k{i}", f"v{i}")
        # First 5 should be evicted (15 - 10 = 5)
        assert cm.l1_get("k0") is None
        assert cm.l1_get("k4") is None
        # k5 onwards should exist
        assert cm.l1_get("k5") == "v5"
        assert cm.l1_get("k14") == "v14"

    def test_l1_thread_safety(self):
        """多线程环境下不应抛出异常。"""
        import threading
        errors = []

        def writer(start):
            try:
                for i in range(100):
                    self.cm.l1_set(f"tk{start}_{i}", f"tv{start}_{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []


# ==================================================================
# L2 缓存测试 (mocked Redis)
# ==================================================================


class TestL2Cache:
    def setup_method(self):
        self.cm = CacheManager()
        self.mock_redis = AsyncMock()
        self.mock_redis.get = AsyncMock(return_value=None)
        self.cm.set_redis_client(type("Obj", (), {"redis": self.mock_redis})())

    @pytest.mark.asyncio
    async def test_l2_miss_without_redis(self):
        cm = CacheManager()
        result = await cm.l2_get("any")
        assert result is None

    @pytest.mark.asyncio
    async def test_l2_set_without_redis(self):
        cm = CacheManager()
        await cm.l2_set("key", "value")
        # Verify the value was NOT stored in L1 (l2_set doesn't backfill L1)
        assert cm.l1_get("key") is None

    @pytest.mark.asyncio
    async def test_l2_get_returns_none_when_no_redis(self):
        cm = CacheManager()
        result = await cm.l2_get("key")
        assert result is None

    @pytest.mark.asyncio
    async def test_l2_roundtrip(self):
        """L2 写入后应能读出（LZ4 压缩/解压往返）。"""
        value = '{"data": "test_value"}'
        compressed = self.cm._compress(value)

        self.mock_redis.get = AsyncMock(return_value=compressed)
        result = await self.cm.l2_get("test-key")
        assert result == value

    @pytest.mark.asyncio
    async def test_l2_large_object_skipped(self):
        big = "x" * (L2_MAX_VALUE_BYTES + 1)
        await self.cm.l2_set("big", big)
        # Should not call redis.set for oversized objects
        self.mock_redis.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_l2_decompress_failure_returns_none(self):
        self.mock_redis.get = AsyncMock(return_value=b"corrupted-data")
        result = await self.cm.l2_get("key")
        assert result is None

    @pytest.mark.asyncio
    async def test_l2_set_with_ttl(self):
        value = "ttl-test"
        self.mock_redis.set = AsyncMock(return_value=None)
        await self.cm.l2_set("ttl-key", value, ttl=1800)
        self.mock_redis.set.assert_called_once()
        call_args = self.mock_redis.set.call_args
        assert call_args[1]["ex"] == 1800 or call_args[0][2] == 1800


# ==================================================================
# L3 缓存测试 (mocked Qdrant)
# ==================================================================


class TestL3Cache:
    def setup_method(self):
        self.cm = CacheManager()
        self.mock_qdrant = MagicMock()
        self.cm.set_qdrant_client(self.mock_qdrant)

    @pytest.mark.asyncio
    async def test_l3_query_no_qdrant(self):
        cm = CacheManager()
        result = await cm.l3_query([0.1] * 1024)
        assert result is None

    @pytest.mark.asyncio
    async def test_l3_store_no_qdrant(self):
        cm = CacheManager()
        await cm.l3_store("hash", "norm", "model", "resp", "uid", 50, [0.1] * 1024)
        # Should not raise; qdrant is None so no embedding stored
        assert cm._qdrant_client is None

    @pytest.mark.asyncio
    async def test_l3_query_returns_none_on_no_result(self):
        self.mock_qdrant.query_vector = AsyncMock(return_value=None)
        result = await self.cm.l3_query([0.1] * 1024, threshold=0.95)
        assert result is None

    @pytest.mark.asyncio
    async def test_l3_query_returns_result_with_payload(self):
        self.mock_qdrant.query_vector = AsyncMock(
            return_value={
                "id": "vec-1",
                "score": 0.97,
                "payload": {
                    "response_json": '{"choices": [...]}',
                    "ttl": int(time.time()) + 86400,
                    "hit_count": 5,
                    "model": "gpt-4",
                },
            }
        )
        self.mock_qdrant.store_embedding = AsyncMock(return_value=None)
        result = await self.cm.l3_query([0.1] * 1024, threshold=0.95)
        assert result is not None
        assert result["response_json"] == '{"choices": [...]}'
        assert result["score"] == 0.97
        assert result["model"] == "gpt-4"
        assert result["hit_count"] == 6  # incremented

    @pytest.mark.asyncio
    async def test_l3_query_expired_ttl_returns_none(self):
        past_ttl = int(time.time()) - 100
        self.mock_qdrant.query_vector = AsyncMock(
            return_value={
                "id": "vec-1",
                "score": 0.97,
                "payload": {"response_json": "resp", "ttl": past_ttl, "hit_count": 0},
            }
        )
        result = await self.cm.l3_query([0.1] * 1024)
        assert result is None

    @pytest.mark.asyncio
    async def test_l3_store_calls_qdrant(self):
        self.mock_qdrant.store_embedding = AsyncMock(return_value=None)
        await self.cm.l3_store(
            prompt_hash="abc123",
            prompt_normalized="Hello world",
            model="gpt-4",
            response_json='{"choices": []}',
            user_id="user-1",
            token_count=200,
            vector=[0.1] * 1024,
        )
        self.mock_qdrant.store_embedding.assert_called_once()
        call_kwargs = self.mock_qdrant.store_embedding.call_args
        assert call_kwargs[1]["collection"] == "semantic_cache"
        payload = call_kwargs[1]["payload"]
        assert payload["model"] == "gpt-4"
        assert payload["hit_count"] == 0
        assert payload["cache_tier"] == "L3"

    @pytest.mark.asyncio
    async def test_l3_store_management_mode_manual(self):
        self.mock_qdrant.store_embedding = AsyncMock(return_value=None)
        await self.cm.l3_store(
            prompt_hash="h", prompt_normalized="n", model="m",
            response_json="r", user_id="u", token_count=10,
            vector=[0.1]*1024, management_mode="manual",
        )
        payload = self.mock_qdrant.store_embedding.call_args[1]["payload"]
        assert payload["management_mode"] == "manual"
        assert payload["ttl"] == 0  # manual mode: no TTL


# ==================================================================
# Backfill 策略测试
# ==================================================================


class TestBackfill:
    def setup_method(self):
        self.cm = CacheManager()

    @pytest.mark.asyncio
    async def test_backfill_on_l2_hit_updates_l1(self):
        """L2 hit 时应回填 L1。"""
        await self.cm.backfill_on_l2_hit("key", "response-value")
        assert self.cm.l1_get("key") == "response-value"

    @pytest.mark.asyncio
    async def test_backfill_on_l3_hit_updates_l1_only(self):
        """L3 hit 时只回填 L1，不回填 L2。"""
        await self.cm.backfill_on_l3_hit("key", "l3-response")
        assert self.cm.l1_get("key") == "l3-response"

    @pytest.mark.asyncio
    async def test_backfill_on_miss(self):
        """MISS 时应回填 L1 + L2。"""
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=None)
        self.cm.set_redis_client(type("Obj", (), {"redis": mock_redis})())
        await self.cm.backfill_on_miss("k", "v", "norm", "m", "u", 50)
        assert self.cm.l1_get("k") == "v"

    @pytest.mark.asyncio
    async def test_backfill_on_miss_skips_l3_for_small_tokens(self):
        """token_count < L3_MIN_TOKEN_COUNT 时跳过 L3 回填。"""
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=None)
        self.cm.set_redis_client(type("Obj", (), {"redis": mock_redis})())
        mock_qdrant = MagicMock()
        self.cm.set_qdrant_client(mock_qdrant)
        await self.cm.backfill_on_miss("k", "v", "norm", "m", "u", 50)
        # No L3 task should be created because token_count=50 < 100
        mock_qdrant.store_embedding.assert_not_called()


# ==================================================================
# get() 多层级联测试
# ==================================================================


class TestCacheGet:
    def setup_method(self):
        self.cm = CacheManager()

    def test_l1_hit_sync(self):
        self.cm.l1_set("hk", "lv1-value")
        result = self.cm.l1_get("hk")
        assert result == "lv1-value"

    @pytest.mark.asyncio
    async def test_get_l1_hit(self):
        self.cm.l1_set("hk", "lv1-value")
        result = await self.cm.get("hk")
        assert result is not None
        assert result["hit_tier"] == "L1"
        assert result["value"] == "lv1-value"

    @pytest.mark.asyncio
    async def test_get_l1_miss_no_value_fn_returns_none(self):
        result = await self.cm.get("no-key")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_l2_hit_backfills_l1(self):
        mock_redis = AsyncMock()
        compressed = self.cm._compress("l2-value")
        mock_redis.get = AsyncMock(return_value=compressed)
        self.cm.set_redis_client(type("Obj", (), {"redis": mock_redis})())

        result = await self.cm.get("l2key")
        assert result is not None
        assert result["hit_tier"] == "L2"
        # L1 should now have the value too
        assert self.cm.l1_get("l2key") == "l2-value"

    @pytest.mark.asyncio
    async def test_get_miss_calls_value_fn(self):
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=None)
        self.cm.set_redis_client(type("Obj", (), {"redis": mock_redis})())

        def value_fn(**kwargs):
            return '({"result": "computed"})', {"token_count": 200}

        result = await self.cm.get("miss-key", value_fn=value_fn)
        assert result is not None
        assert result["hit_tier"] == "MISS"

    @pytest.mark.asyncio
    async def test_get_with_vector_l3_miss(self):
        """L1/L2 miss 且 vector 参数传入但 L3 也 miss 时应返回 None。"""
        self.cm._qdrant_client = MagicMock()
        self.cm._qdrant_client.query_vector = AsyncMock(return_value=None)
        result = await self.cm.get("k", vector=[0.1]*1024)
        assert result is None


# ==================================================================
# L3CleanupScheduler 测试
# ==================================================================


class TestL3CleanupScheduler:
    def setup_method(self):
        self.cm = CacheManager()
        self.scheduler = L3CleanupScheduler(self.cm, interval_minutes=1)

    def test_initial_task_is_none(self):
        assert self.scheduler._task is None

    def test_update_interval(self):
        self.scheduler.update_interval(30)
        assert self.scheduler._interval_minutes == 30

    @pytest.mark.asyncio
    async def test_start_creates_task(self):
        await self.scheduler.start()
        assert self.scheduler._task is not None
        assert not self.scheduler._task.done()
        await self.scheduler.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self):
        await self.scheduler.start()
        await self.scheduler.stop()
        assert self.scheduler._task is None

    @pytest.mark.asyncio
    async def test_double_start_is_noop(self):
        await self.scheduler.start()
        task_id = id(self.scheduler._task)
        await self.scheduler.start()
        assert id(self.scheduler._task) == task_id
        await self.scheduler.stop()


# ==================================================================
# LightweightReranker 测试
# ==================================================================


class TestLightweightReranker:
    @pytest.mark.asyncio
    async def test_rerank_identical_documents(self):
        reranker = LightweightReranker()
        scores = await reranker.rerank("hello world", ["hello world", "goodbye world"])
        assert len(scores) == 2
        assert scores[0] > scores[1]

    @pytest.mark.asyncio
    async def test_rerank_no_overlap(self):
        reranker = LightweightReranker()
        scores = await reranker.rerank("quantum computing", ["cooking recipes", "gardening tips"])
        assert all(s == 0.0 for s in scores)

    @pytest.mark.asyncio
    async def test_rerank_single_document(self):
        reranker = LightweightReranker()
        scores = await reranker.rerank("test", ["test"])
        assert scores == [1.0]


# ==================================================================
# SemanticCacheWithRerank 测试
# ==================================================================


class TestSemanticCacheWithRerank:
    def setup_method(self):
        self.cm = CacheManager()
        self.reranker = LightweightReranker()
        self.query = SemanticCacheWithRerank(
            self.cm, reranker=self.reranker, retrieve_top_k=3
        )

    @pytest.mark.asyncio
    async def test_no_qdrant_returns_none(self):
        cm = CacheManager()
        q = SemanticCacheWithRerank(cm)
        result = await q.query_with_rerank("test", [0.1] * 1024)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_candidates_returns_none(self):
        cm = CacheManager()
        mock_qdrant = MagicMock()
        mock_qdrant.query_vector_multi = AsyncMock(return_value=[])
        cm.set_qdrant_client(mock_qdrant)
        q = SemanticCacheWithRerank(cm, reranker=self.reranker)
        result = await q.query_with_rerank("test", [0.1] * 1024)
        assert result is None

    @pytest.mark.asyncio
    async def test_below_rerank_threshold_returns_none(self):
        cm = CacheManager()
        mock_qdrant = MagicMock()
        future = asyncio.Future()
        future.set_result([{
            "id": "v1",
            "score": 0.92,
            "payload": {
                "response_json": '{"answer": "yes"}',
                "prompt_normalized": "different topic entirely",
                "ttl": int(time.time()) + 86400,
                "hit_count": 0,
            },
        }])
        mock_qdrant.query_vector_multi = AsyncMock(return_value=[{
            "id": "v1",
            "score": 0.92,
            "payload": {
                "response_json": '{"answer": "yes"}',
                "prompt_normalized": "different topic entirely",
                "ttl": int(time.time()) + 86400,
                "hit_count": 0,
            },
        }])
        cm.set_qdrant_client(mock_qdrant)
        q = SemanticCacheWithRerank(cm, reranker=self.reranker, rerank_threshold=0.99)
        result = await q.query_with_rerank("test", [0.1] * 1024)
        assert result is None

    @pytest.mark.asyncio
    async def test_expired_ttl_returns_none(self):
        cm = CacheManager()
        past = int(time.time()) - 1000
        mock_qdrant = MagicMock()
        mock_qdrant.query_vector_multi = AsyncMock(return_value=[{
            "id": "v1",
            "score": 0.95,
            "payload": {
                "response_json": "old",
                "prompt_normalized": "old",
                "ttl": past,
                "hit_count": 0,
            },
        }])
        cm.set_qdrant_client(mock_qdrant)
        q = SemanticCacheWithRerank(cm)
        result = await q.query_with_rerank("test", [0.1] * 1024)
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_rerank_return(self):
        cm = CacheManager()
        now = int(time.time())
        mock_qdrant = MagicMock()
        mock_qdrant.query_vector_multi = AsyncMock(return_value=[{
            "id": "v1",
            "score": 0.93,
            "payload": {
                "response_json": '{"answer": "re-ranked"}',
                "prompt_normalized": "matching topic",
                "ttl": now + 86400,
                "hit_count": 3,
            },
        }])
        cm.set_qdrant_client(mock_qdrant)
        reranker = LightweightReranker()
        q = SemanticCacheWithRerank(cm, reranker=reranker, rerank_threshold=0.0)
        result = await q.query_with_rerank("matching", [0.1] * 1024)
        assert result is not None
        assert result["response_json"] == '{"answer": "re-ranked"}'


# ==================================================================
# _emit_cache_debug 测试
# ==================================================================


class TestEmitCacheDebug:
    def test_emits_nothing_without_collector(self):
        """没有 TraceCollector 时应静默返回。"""
        # Should not raise even when TraceCollector is unavailable
        _emit_cache_debug("key", "L1", time.monotonic())
        assert True  # If we get here, no exception was raised
