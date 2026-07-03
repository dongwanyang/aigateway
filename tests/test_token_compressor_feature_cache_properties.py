"""
Property-Based Tests for Token Compressor and Feature Cache
============================================================

使用 pytest.mark.parametrize 模拟属性测试，验证 Token 压缩和特征缓存的核心属性。

Properties:
- Property 13: Token 压缩故障透传 — 不支持格式或超时时输出原始图像
- Property 14: Feature Vector 维度约束 — 输出维度不超过 max_vector_dimensions
- Property 15: Token 节省计算公式正确性 — original = size/4, compressed = dimensions
- Property 16: 特征缓存存取一致性 — 存入的向量可完整取出
- Property 17: 特征缓存 API Key 隔离 — 不同 API Key 同名 character 互不污染
- Property 18: 缓存命中 TTL 续期 — 每次命中后 TTL 被延长

**Validates: Requirements 4.3-4.6, 5.1-5.7**
"""

import asyncio
import json
import os
import random
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.generation_optimization.config import (
    FeatureCacheConfig,
    TokenCompressorConfig,
)
from aigateway_core.generation_optimization.models import CompressionResult
from aigateway_core.generation_optimization.strategies.feature_cache import (
    FeatureCacheManager,
)
from aigateway_core.generation_optimization.strategies.token_compressor import (
    TokenCompressorStrategy,
)
from aigateway_core.media.types import MediaContent, MediaType


# ==================================================================
# Fixtures
# ==================================================================


@pytest.fixture
def default_compressor_config():
    """Default TokenCompressorConfig."""
    return TokenCompressorConfig(
        enabled=True,
        target_compression_ratio=0.5,
        min_compression_ratio=0.2,
        max_compression_ratio=0.9,
        max_vector_dimensions=512,
        timeout_seconds=30.0,
        supported_formats=["image/png", "image/jpeg", "image/webp", "image/bmp"],
        max_images_per_request=10,
        max_image_size_bytes=20 * 1024 * 1024,
    )


@pytest.fixture
def compressor(default_compressor_config):
    """TokenCompressorStrategy instance."""
    return TokenCompressorStrategy(config=default_compressor_config)


@pytest.fixture
def feature_cache_config():
    """Default FeatureCacheConfig."""
    return FeatureCacheConfig(
        enabled=True,
        ttl_days=30,
        lookup_timeout_ms=500,
        extraction_model_version="clip-vit-large-patch14",
    )


@pytest.fixture
def mock_redis():
    """Mock Redis with in-memory store for realistic testing."""
    store = {}
    redis_mock = AsyncMock()

    async def mock_get(key):
        return store.get(key)

    async def mock_set(key, value, ex=None):
        store[key] = value if isinstance(value, bytes) else value.encode("utf-8") if isinstance(value, str) else value
        return True

    async def mock_expire(key, ttl):
        return True

    redis_mock.get = AsyncMock(side_effect=mock_get)
    redis_mock.set = AsyncMock(side_effect=mock_set)
    redis_mock.expire = AsyncMock(side_effect=mock_expire)
    redis_mock._store = store  # expose for test assertions
    return redis_mock


@pytest.fixture
def mock_redis_client(mock_redis):
    """Mock RedisClientManager with .redis attribute."""
    client = MagicMock()
    client.redis = mock_redis
    return client


@pytest.fixture
def feature_cache(mock_redis_client, feature_cache_config):
    """FeatureCacheManager instance with mocked Redis."""
    return FeatureCacheManager(mock_redis_client, feature_cache_config)


# ==================================================================
# Property 13: Token 压缩故障透传
# **Validates: Requirements 4.3-4.6**
# ==================================================================

# Generate many unsupported MIME types and edge cases
_UNSUPPORTED_MIME_TYPES = [
    "image/tiff",
    "image/gif",
    "image/svg+xml",
    "video/mp4",
    None,
    "audio/wav",
    "application/pdf",
    "text/plain",
    "image/x-icon",
    "image/heic",
    "image/heif",
    "video/webm",
    "application/octet-stream",
    "image/avif",
    "image/jxl",
]

_UNSUPPORTED_SIZES = [0, 1, 100, 1024, 4096, 8192, 65536, 1_000_000, 5_000_000]

# Create parametrized inputs: (mime_type, size_bytes)
_PASSTHROUGH_PARAMS = [
    (mime, size)
    for mime in _UNSUPPORTED_MIME_TYPES
    for size in random.sample(_UNSUPPORTED_SIZES, min(3, len(_UNSUPPORTED_SIZES)))
]


class TestProperty13TokenCompressionPassthrough:
    """Property 13: Token 压缩故障透传.

    For ANY image with unsupported mime_type or when compression times out,
    the output must have feature_vector == [], compressed_token_count == original_token_count,
    and compression_ratio == 1.0.

    **Validates: Requirements 4.3-4.6**
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "mime_type,size_bytes",
        _PASSTHROUGH_PARAMS,
        ids=[f"{m}-{s}B" for m, s in _PASSTHROUGH_PARAMS],
    )
    async def test_unsupported_format_passthrough(
        self, compressor, default_compressor_config, mime_type, size_bytes
    ):
        """Unsupported formats must produce passthrough results."""
        image = MediaContent(
            media_type=MediaType.IMAGE,
            mime_type=mime_type,
            size_bytes=size_bytes,
            raw_data=b"\x00" * min(size_bytes, 1024) if size_bytes > 0 else b"",
        )

        result = await compressor.compress(image, default_compressor_config)

        assert result.feature_vector == [], (
            f"Expected empty feature_vector for unsupported mime_type={mime_type}"
        )
        assert result.compressed_token_count == result.original_token_count, (
            f"Passthrough must have compressed == original tokens"
        )
        assert result.compression_ratio == 1.0, (
            f"Passthrough must have compression_ratio=1.0"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "size_bytes",
        [512, 2048, 4096, 10000, 50000, 100000],
    )
    async def test_timeout_passthrough(self, size_bytes):
        """Timeout during compression must produce passthrough results."""
        config = TokenCompressorConfig(
            timeout_seconds=0.001,  # Very short timeout to force timeout
            supported_formats=["image/png", "image/jpeg", "image/webp", "image/bmp"],
        )
        compressor = TokenCompressorStrategy(config=config)

        image = MediaContent(
            media_type=MediaType.IMAGE,
            mime_type="image/png",
            size_bytes=size_bytes,
            raw_data=b"\x89PNG" + b"\x00" * (min(size_bytes, 1024) - 4),
        )

        # Patch _do_compress to simulate slow processing
        original_do_compress = compressor._do_compress

        async def slow_compress(*args, **kwargs):
            await asyncio.sleep(1.0)
            return await original_do_compress(*args, **kwargs)

        compressor._do_compress = slow_compress

        result = await compressor.compress(image, config)

        assert result.feature_vector == [], (
            "Timeout must produce empty feature_vector"
        )
        assert result.compressed_token_count == result.original_token_count, (
            "Timeout passthrough must have compressed == original tokens"
        )
        assert result.compression_ratio == 1.0, (
            "Timeout passthrough must have compression_ratio=1.0"
        )


# ==================================================================
# Property 14: Feature Vector 维度约束
# **Validates: Requirements 4.3-4.6**
# ==================================================================

# Test with various image sizes and max_vector_dimensions configs
_DIMENSION_TEST_PARAMS = [
    # (size_bytes, max_vector_dimensions)
    (1024, 64),
    (1024, 128),
    (1024, 256),
    (1024, 512),
    (4096, 32),
    (4096, 64),
    (4096, 512),
    (4096, 1024),
    (10000, 128),
    (10000, 256),
    (10000, 512),
    (50000, 64),
    (50000, 256),
    (50000, 512),
    (100000, 128),
    (100000, 512),
    (100000, 1024),
    (500000, 256),
    (500000, 512),
    (1_000_000, 128),
    (1_000_000, 512),
    (1_000_000, 1024),
    (5_000_000, 256),
    (5_000_000, 512),
    (10_000_000, 512),
    (10_000_000, 1024),
    (10_000_000, 2048),
]


class TestProperty14FeatureVectorDimensionConstraint:
    """Property 14: Feature Vector 维度约束.

    For ANY successful compression (supported format, within size limits),
    len(feature_vector) <= config.max_vector_dimensions.

    **Validates: Requirements 4.3-4.6**
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "size_bytes,max_dims",
        _DIMENSION_TEST_PARAMS,
        ids=[f"{s}B-max{d}" for s, d in _DIMENSION_TEST_PARAMS],
    )
    async def test_vector_dimensions_within_limit(self, size_bytes, max_dims):
        """Feature vector length must never exceed max_vector_dimensions."""
        config = TokenCompressorConfig(
            max_vector_dimensions=max_dims,
            target_compression_ratio=0.5,
            max_image_size_bytes=20 * 1024 * 1024,
            supported_formats=["image/png", "image/jpeg", "image/webp", "image/bmp"],
        )
        compressor = TokenCompressorStrategy(config=config)

        image = MediaContent(
            media_type=MediaType.IMAGE,
            mime_type="image/png",
            size_bytes=size_bytes,
            raw_data=b"\x89PNG" + os.urandom(min(size_bytes, 1024) - 4),
        )

        result = await compressor.compress(image, config)

        assert len(result.feature_vector) <= max_dims, (
            f"Feature vector length {len(result.feature_vector)} exceeds "
            f"max_vector_dimensions={max_dims} for size_bytes={size_bytes}"
        )
        # For successful compressions, vector should be non-empty
        assert len(result.feature_vector) > 0, (
            "Successful compression must produce non-empty feature vector"
        )


# ==================================================================
# Property 15: Token 节省计算公式正确性
# **Validates: Requirements 4.3-4.6**
# ==================================================================

_TOKEN_CALC_SIZES = [
    0, 1, 2, 3, 4, 7, 8, 15, 16, 100, 255, 256, 512, 1000, 1024,
    2048, 4096, 8192, 10000, 65536, 100000, 500000, 1_000_000,
    5_000_000, 10_000_000, 19_000_000,
]


class TestProperty15TokenCalculationFormula:
    """Property 15: Token 节省计算公式正确性.

    For ANY compression result:
    - original_token_count == image.size_bytes // 4
    - compressed_token_count == len(feature_vector)

    **Validates: Requirements 4.3-4.6**
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("size_bytes", _TOKEN_CALC_SIZES)
    async def test_token_formula_supported_format(self, size_bytes):
        """Token formula: original=size//4, compressed=len(feature_vector) for supported formats."""
        config = TokenCompressorConfig(
            max_vector_dimensions=512,
            target_compression_ratio=0.5,
            max_image_size_bytes=20 * 1024 * 1024,
            supported_formats=["image/png"],
        )
        compressor = TokenCompressorStrategy(config=config)

        image = MediaContent(
            media_type=MediaType.IMAGE,
            mime_type="image/png",
            size_bytes=size_bytes,
            raw_data=b"\x89PNG" + b"\xab" * max(0, min(size_bytes, 1024) - 4),
        )

        result = await compressor.compress(image, config)

        assert result.original_token_count == size_bytes // 4, (
            f"original_token_count should be {size_bytes // 4}, "
            f"got {result.original_token_count}"
        )
        assert result.compressed_token_count == len(result.feature_vector), (
            f"compressed_token_count should equal len(feature_vector)="
            f"{len(result.feature_vector)}, got {result.compressed_token_count}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("size_bytes", _TOKEN_CALC_SIZES)
    async def test_token_formula_unsupported_format(self, size_bytes):
        """Token formula holds for passthrough (unsupported format) results too."""
        config = TokenCompressorConfig(
            max_vector_dimensions=512,
            supported_formats=["image/png"],
        )
        compressor = TokenCompressorStrategy(config=config)

        image = MediaContent(
            media_type=MediaType.IMAGE,
            mime_type="image/tiff",  # Unsupported
            size_bytes=size_bytes,
            raw_data=b"\x00" * min(size_bytes, 100),
        )

        result = await compressor.compress(image, config)

        assert result.original_token_count == size_bytes // 4, (
            f"original_token_count should be {size_bytes // 4}, "
            f"got {result.original_token_count}"
        )
        # For passthrough: feature_vector is [], so compressed = original
        assert result.compressed_token_count == len(result.feature_vector) or (
            result.feature_vector == []
            and result.compressed_token_count == result.original_token_count
        ), (
            f"compressed_token_count mismatch: "
            f"got {result.compressed_token_count}, "
            f"feature_vector len={len(result.feature_vector)}"
        )


# ==================================================================
# Property 16: 特征缓存存取一致性
# **Validates: Requirements 5.1-5.7**
# ==================================================================

# Generate various test vectors
random.seed(42)
_CACHE_VECTORS = [
    [0.0],
    [1.0, -1.0],
    [0.5] * 10,
    [random.uniform(-1.0, 1.0) for _ in range(64)],
    [random.uniform(-1.0, 1.0) for _ in range(128)],
    [random.uniform(-1.0, 1.0) for _ in range(256)],
    [random.uniform(-1.0, 1.0) for _ in range(512)],
    [float(i) / 100.0 for i in range(50)],
    [0.0] * 512,
    [1e-10, -1e-10, 1e10, -1e10, 0.0],
    [random.uniform(-100.0, 100.0) for _ in range(32)],
    [random.uniform(-0.001, 0.001) for _ in range(100)],
]


class TestProperty16FeatureCacheConsistency:
    """Property 16: 特征缓存存取一致性.

    For ANY vector stored in FeatureCacheManager, a subsequent get_feature()
    with the same key returns the exact same vector.

    **Validates: Requirements 5.1-5.7**
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "vector",
        _CACHE_VECTORS,
        ids=[f"vec-len{len(v)}" for v in _CACHE_VECTORS],
    )
    async def test_store_then_get_returns_same_vector(
        self, feature_cache, mock_redis, vector
    ):
        """Stored vector must be retrievable without data loss."""
        api_key = "test_key_001"
        char_id = "character_alpha"
        model_ver = "clip-vit-large-patch14"

        await feature_cache.store_feature(api_key, char_id, model_ver, vector)

        result = await feature_cache.get_feature(api_key, char_id, model_ver)

        assert result is not None, "get_feature should return the stored vector"
        assert len(result) == len(vector), (
            f"Vector length mismatch: stored {len(vector)}, got {len(result)}"
        )
        for i, (expected, actual) in enumerate(zip(vector, result)):
            assert expected == pytest.approx(actual, abs=1e-15), (
                f"Vector element [{i}] mismatch: expected {expected}, got {actual}"
            )


# ==================================================================
# Property 17: 特征缓存 API Key 隔离
# **Validates: Requirements 5.1-5.7**
# ==================================================================

_API_KEY_PAIRS = [
    ("api_key_A", "api_key_B"),
    ("user_123", "user_456"),
    ("project-alpha", "project-beta"),
    ("key_with_special!@#", "key_with_other$%^"),
    ("a", "b"),
    ("very_long_api_key_" + "x" * 100, "very_long_api_key_" + "y" * 100),
]

_ISOLATION_VECTORS_A = [
    [1.0, 2.0, 3.0],
    [0.1] * 64,
    [random.uniform(-1, 1) for _ in range(128)],
    [float(i) for i in range(10)],
    [-1.0, -2.0, -3.0],
    [0.0] * 50,
]

_ISOLATION_VECTORS_B = [
    [4.0, 5.0, 6.0],
    [0.9] * 64,
    [random.uniform(-1, 1) for _ in range(128)],
    [float(-i) for i in range(10)],
    [1.0, 2.0, 3.0],
    [1.0] * 50,
]


class TestProperty17APIKeyIsolation:
    """Property 17: 特征缓存 API Key 隔离.

    For ANY two distinct api_key_ids with the same character_id and model_version,
    their stored vectors do NOT interfere.

    **Validates: Requirements 5.1-5.7**
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "key_pair,vec_a,vec_b",
        list(zip(_API_KEY_PAIRS, _ISOLATION_VECTORS_A, _ISOLATION_VECTORS_B)),
        ids=[f"{pair[0][:10]}-vs-{pair[1][:10]}" for pair in _API_KEY_PAIRS],
    )
    async def test_different_api_keys_isolated(
        self, feature_cache, mock_redis, key_pair, vec_a, vec_b
    ):
        """Different API keys with same character_id must store/retrieve independently."""
        key_a, key_b = key_pair
        char_id = "shared_character"
        model_ver = "clip-vit-large-patch14"

        # Store vector A for key A
        await feature_cache.store_feature(key_a, char_id, model_ver, vec_a)

        # Store vector B for key B
        await feature_cache.store_feature(key_b, char_id, model_ver, vec_b)

        # Retrieve for key A — should get vec_a
        result_a = await feature_cache.get_feature(key_a, char_id, model_ver)
        assert result_a is not None, f"get_feature for {key_a} should not be None"
        assert len(result_a) == len(vec_a)
        for i, (expected, actual) in enumerate(zip(vec_a, result_a)):
            assert expected == pytest.approx(actual, abs=1e-15), (
                f"key_a vector [{i}] mismatch"
            )

        # Retrieve for key B — should get vec_b
        result_b = await feature_cache.get_feature(key_b, char_id, model_ver)
        assert result_b is not None, f"get_feature for {key_b} should not be None"
        assert len(result_b) == len(vec_b)
        for i, (expected, actual) in enumerate(zip(vec_b, result_b)):
            assert expected == pytest.approx(actual, abs=1e-15), (
                f"key_b vector [{i}] mismatch"
            )


# ==================================================================
# Property 18: 缓存命中 TTL 续期
# **Validates: Requirements 5.1-5.7**
# ==================================================================

_TTL_CONFIGS = [
    7,   # 7 days
    14,  # 14 days
    30,  # 30 days (default)
    60,  # 60 days
    90,  # 90 days
    365, # 1 year
]

# The extend_ttl default parameter is 30 days when called from get_feature()
_EXTEND_TTL_DEFAULT_DAYS = 30


class TestProperty18CacheHitTTLExtension:
    """Property 18: 缓存命中 TTL 续期.

    When get_feature() returns a cached vector, redis.expire() must be called
    to extend the entry's lifetime. The implementation calls extend_ttl() with
    the default ttl_days=30 on each cache hit.

    **Validates: Requirements 5.1-5.7**
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ttl_days", _TTL_CONFIGS)
    async def test_cache_hit_triggers_ttl_extension(self, ttl_days):
        """Every cache hit must call expire() to extend TTL."""
        config = FeatureCacheConfig(
            enabled=True,
            ttl_days=ttl_days,
            lookup_timeout_ms=500,
            extraction_model_version="clip-vit-large-patch14",
        )

        # Setup mock with stored data
        redis_mock = AsyncMock()
        vector = [0.1, 0.2, 0.3, 0.4, 0.5]
        redis_mock.get = AsyncMock(return_value=json.dumps(vector).encode("utf-8"))
        redis_mock.expire = AsyncMock(return_value=True)

        client = MagicMock()
        client.redis = redis_mock

        cache = FeatureCacheManager(client, config)

        api_key = "test_key"
        char_id = "test_char"
        model_ver = "clip-vit-large-patch14"

        result = await cache.get_feature(api_key, char_id, model_ver)

        # Wait for the background TTL extension task
        await asyncio.sleep(0.05)

        assert result == vector, "Should return the cached vector"
        redis_mock.expire.assert_called_once()

        call_args = redis_mock.expire.call_args[0]
        expected_key = f"aigateway:feature:{api_key}:{char_id}:{model_ver}"
        # extend_ttl is called with its default ttl_days=30 from get_feature()
        expected_ttl = _EXTEND_TTL_DEFAULT_DAYS * 86400

        assert call_args[0] == expected_key, (
            f"expire() called with wrong key: {call_args[0]}"
        )
        assert call_args[1] == expected_ttl, (
            f"expire() called with wrong TTL: {call_args[1]}, expected {expected_ttl}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("hit_count", [1, 2, 3, 5])
    async def test_multiple_hits_extend_ttl_each_time(self, hit_count):
        """Each cache hit must independently trigger TTL extension."""
        config = FeatureCacheConfig(
            enabled=True,
            ttl_days=30,
            lookup_timeout_ms=500,
            extraction_model_version="clip-vit-large-patch14",
        )

        redis_mock = AsyncMock()
        vector = [1.0, 2.0, 3.0]
        redis_mock.get = AsyncMock(return_value=json.dumps(vector).encode("utf-8"))
        redis_mock.expire = AsyncMock(return_value=True)

        client = MagicMock()
        client.redis = redis_mock

        cache = FeatureCacheManager(client, config)

        for _ in range(hit_count):
            result = await cache.get_feature("key", "char", "v1")
            assert result == vector
            # Wait for each background task to complete
            await asyncio.sleep(0.05)

        assert redis_mock.expire.call_count == hit_count, (
            f"Expected {hit_count} expire() calls, got {redis_mock.expire.call_count}"
        )
