"""
Tests for FeatureCacheManager — 特征向量缓存管理器
===================================================

验证:
- Key 格式正确: aigateway:feature:{api_key_id}:{character_id}:{model_version}
- get_feature: 命中时返回反序列化的向量
- get_feature: 命中时自动续期 TTL
- get_feature: 超时时返回 None
- get_feature: Redis 不可用时返回 None
- store_feature: 序列化 vector 并设置 TTL
- store_feature: Redis 不可用时静默失败
- extend_ttl: 正确调用 expire
- API Key 隔离: 不同 API Key 同名 character_id 生成不同 key

需求: 5.1, 5.2, 5.4, 5.5, 5.6, 5.7
"""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.pipelines.generation._common.config import FeatureCacheConfig
from aigateway_core.pipelines.generation.token.feature_cache import (
    FeatureCacheManager,
)


# ==================================================================
# Fixtures
# ==================================================================


@pytest.fixture
def config():
    """Default FeatureCacheConfig."""
    return FeatureCacheConfig(
        enabled=True,
        ttl_days=30,
        lookup_timeout_ms=500,
        extraction_model_version="clip-vit-large-patch14",
    )


@pytest.fixture
def mock_redis():
    """Mock Redis client with async methods."""
    redis_mock = AsyncMock()
    redis_mock.get = AsyncMock(return_value=None)
    redis_mock.set = AsyncMock(return_value=True)
    redis_mock.expire = AsyncMock(return_value=True)
    return redis_mock


@pytest.fixture
def mock_redis_client(mock_redis):
    """Mock RedisClientManager with .redis attribute."""
    client = MagicMock()
    client.redis = mock_redis
    return client


@pytest.fixture
def cache(mock_redis_client, config):
    """FeatureCacheManager instance with mocked Redis."""
    return FeatureCacheManager(mock_redis_client, config)


# ==================================================================
# Key Construction Tests
# ==================================================================


class TestBuildKey:
    """Tests for _build_key method."""

    def test_key_format(self, cache):
        """Key follows aigateway:feature:{api_key_id}:{character_id}:{model_version}."""
        key = cache._build_key("key123", "char_01", "clip-vit-large-patch14")
        assert key == "aigateway:feature:key123:char_01:clip-vit-large-patch14"

    def test_different_api_keys_produce_different_keys(self, cache):
        """Different API Keys with same character_id produce different keys (Req 5.7)."""
        key_a = cache._build_key("api_key_A", "hero_char", "v1")
        key_b = cache._build_key("api_key_B", "hero_char", "v1")
        assert key_a != key_b
        assert "api_key_A" in key_a
        assert "api_key_B" in key_b

    def test_key_prefix_constant(self, cache):
        """KEY_PREFIX is correctly set."""
        assert FeatureCacheManager.KEY_PREFIX == "aigateway:feature"


# ==================================================================
# get_feature Tests
# ==================================================================


class TestGetFeature:
    """Tests for get_feature method."""

    @pytest.mark.asyncio
    async def test_cache_hit_returns_vector(self, cache, mock_redis):
        """Cache hit deserializes and returns vector."""
        vector = [0.1, 0.2, 0.3, 0.4, 0.5]
        mock_redis.get.return_value = json.dumps(vector).encode("utf-8")

        result = await cache.get_feature("key1", "char1", "v1")
        assert result == vector

    @pytest.mark.asyncio
    async def test_cache_miss_returns_none(self, cache, mock_redis):
        """Cache miss returns None."""
        mock_redis.get.return_value = None

        result = await cache.get_feature("key1", "char1", "v1")
        assert result is None

    @pytest.mark.asyncio
    async def test_cache_hit_extends_ttl(self, cache, mock_redis):
        """Cache hit triggers TTL extension (Req 5.4)."""
        vector = [1.0, 2.0]
        mock_redis.get.return_value = json.dumps(vector).encode("utf-8")

        await cache.get_feature("key1", "char1", "v1")

        # Give the background task a chance to run
        await asyncio.sleep(0.05)

        mock_redis.expire.assert_called_once()
        call_args = mock_redis.expire.call_args
        assert call_args[0][0] == "aigateway:feature:key1:char1:v1"
        assert call_args[0][1] == 30 * 86400  # 30 days in seconds

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self, cache, mock_redis):
        """Timeout during Redis GET returns None (Req 5.5)."""

        async def slow_get(key):
            await asyncio.sleep(2.0)
            return json.dumps([1.0]).encode()

        mock_redis.get = slow_get

        result = await cache.get_feature("key1", "char1", "v1", timeout_ms=50)
        assert result is None

    @pytest.mark.asyncio
    async def test_redis_none_returns_none(self, mock_redis_client, config):
        """Redis client.redis is None → returns None."""
        mock_redis_client.redis = None
        cache = FeatureCacheManager(mock_redis_client, config)

        result = await cache.get_feature("key1", "char1", "v1")
        assert result is None

    @pytest.mark.asyncio
    async def test_redis_exception_returns_none(self, cache, mock_redis):
        """Redis exception during GET returns None (graceful degradation)."""
        mock_redis.get.side_effect = ConnectionError("Redis down")

        result = await cache.get_feature("key1", "char1", "v1")
        assert result is None

    @pytest.mark.asyncio
    async def test_uses_correct_key(self, cache, mock_redis):
        """get_feature queries Redis with the correct key."""
        mock_redis.get.return_value = None
        await cache.get_feature("mykey", "mychar", "clip-v2")

        mock_redis.get.assert_called_once_with("aigateway:feature:mykey:mychar:clip-v2")


# ==================================================================
# store_feature Tests
# ==================================================================


class TestStoreFeature:
    """Tests for store_feature method."""

    @pytest.mark.asyncio
    async def test_stores_serialized_vector(self, cache, mock_redis):
        """Stores vector as JSON with correct TTL."""
        vector = [0.1, 0.2, 0.3]
        await cache.store_feature("key1", "char1", "v1", vector, ttl_days=30)

        mock_redis.set.assert_called_once_with(
            "aigateway:feature:key1:char1:v1",
            json.dumps(vector),
            ex=30 * 86400,
        )

    @pytest.mark.asyncio
    async def test_custom_ttl(self, cache, mock_redis):
        """Respects custom ttl_days parameter."""
        await cache.store_feature("k", "c", "v", [1.0], ttl_days=7)

        call_args = mock_redis.set.call_args
        assert call_args[1]["ex"] == 7 * 86400

    @pytest.mark.asyncio
    async def test_redis_none_silently_fails(self, mock_redis_client, config):
        """Redis client.redis is None → does nothing."""
        mock_redis_client.redis = None
        cache = FeatureCacheManager(mock_redis_client, config)

        # Should not raise
        await cache.store_feature("key1", "char1", "v1", [1.0, 2.0])

    @pytest.mark.asyncio
    async def test_redis_exception_silently_fails(self, cache, mock_redis):
        """Redis exception during SET is caught and logged."""
        mock_redis.set.side_effect = ConnectionError("Redis down")

        # Should not raise
        await cache.store_feature("key1", "char1", "v1", [1.0])


# ==================================================================
# extend_ttl Tests
# ==================================================================


class TestExtendTtl:
    """Tests for extend_ttl method."""

    @pytest.mark.asyncio
    async def test_calls_expire_with_correct_args(self, cache, mock_redis):
        """Calls Redis EXPIRE with correct key and TTL."""
        await cache.extend_ttl("key1", "char1", "v1", ttl_days=30)

        mock_redis.expire.assert_called_once_with(
            "aigateway:feature:key1:char1:v1",
            30 * 86400,
        )

    @pytest.mark.asyncio
    async def test_custom_ttl_days(self, cache, mock_redis):
        """Respects custom ttl_days for extend."""
        await cache.extend_ttl("k", "c", "v", ttl_days=60)

        call_args = mock_redis.expire.call_args
        assert call_args[0][1] == 60 * 86400

    @pytest.mark.asyncio
    async def test_redis_none_does_nothing(self, mock_redis_client, config):
        """Redis client.redis is None → does nothing."""
        mock_redis_client.redis = None
        cache = FeatureCacheManager(mock_redis_client, config)

        # Should not raise
        await cache.extend_ttl("key1", "char1", "v1")

    @pytest.mark.asyncio
    async def test_redis_exception_silently_fails(self, cache, mock_redis):
        """Redis exception during EXPIRE is caught."""
        mock_redis.expire.side_effect = ConnectionError("Redis down")

        # Should not raise
        await cache.extend_ttl("key1", "char1", "v1")
