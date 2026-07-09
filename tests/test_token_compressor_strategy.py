"""
Tests for TokenCompressorStrategy — 视觉 Token 压缩核心逻辑
==========================================================

验证:
- 正常压缩: 支持格式的图片被压缩为 feature vector
- Feature vector 维度不超过 max_vector_dimensions
- 不支持格式: 透传原图（compression_ratio=1.0, feature_vector=[]）
- 超大图片: 超过 max_image_size_bytes 时透传
- 超时处理: 超时后透传原图
- Token 计算: original = size_bytes/4, compressed = len(feature_vector)
- 批量压缩: 验证 max_images_per_request 限制
- 确定性: 相同输入产生相同输出

需求: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.pipelines.generation._common.config import TokenCompressorConfig
from aigateway_core.pipelines.generation._common.exceptions import TokenCompressionError
from aigateway_core.pipelines.generation._common.models import CompressionResult
from aigateway_core.pipelines.generation.token.token_compressor import (
    TokenCompressorStrategy,
)
from aigateway_core.prefix.media.types import MediaContent, MediaType


@pytest.fixture
def default_config():
    """Default Token Compressor config."""
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
def compressor(default_config):
    """Create a TokenCompressorStrategy instance."""
    return TokenCompressorStrategy(config=default_config)


@pytest.fixture
def sample_png_image():
    """Create a sample PNG image MediaContent."""
    return MediaContent(
        media_type=MediaType.IMAGE,
        mime_type="image/png",
        size_bytes=4096,
        raw_data=b"\x89PNG" + b"\x00" * 4092,
    )


@pytest.fixture
def sample_jpeg_image():
    """Create a sample JPEG image MediaContent."""
    return MediaContent(
        media_type=MediaType.IMAGE,
        mime_type="image/jpeg",
        size_bytes=8192,
        raw_data=b"\xff\xd8\xff" + b"\x00" * 8189,
    )


@pytest.fixture
def unsupported_image():
    """Create an unsupported format image MediaContent."""
    return MediaContent(
        media_type=MediaType.IMAGE,
        mime_type="image/tiff",
        size_bytes=4096,
        raw_data=b"\x49\x49" + b"\x00" * 4094,
    )


@pytest.fixture
def oversized_image():
    """Create an oversized image MediaContent."""
    return MediaContent(
        media_type=MediaType.IMAGE,
        mime_type="image/png",
        size_bytes=25 * 1024 * 1024,  # 25 MB > 20 MB limit
        raw_data=None,
        source_url="https://example.com/large.png",
    )


class TestTokenCompressorCompress:
    """Tests for the compress() method."""

    @pytest.mark.asyncio
    async def test_compress_supported_png(self, compressor, sample_png_image, default_config):
        """Supported PNG format should be compressed successfully."""
        result = await compressor.compress(sample_png_image, default_config)

        assert isinstance(result, CompressionResult)
        assert len(result.feature_vector) > 0
        assert len(result.feature_vector) <= default_config.max_vector_dimensions
        assert result.original_token_count == sample_png_image.size_bytes // 4
        assert result.compressed_token_count == len(result.feature_vector)
        assert 0.0 <= result.compression_ratio <= 1.0

    @pytest.mark.asyncio
    async def test_compress_supported_jpeg(self, compressor, sample_jpeg_image, default_config):
        """Supported JPEG format should be compressed successfully."""
        result = await compressor.compress(sample_jpeg_image, default_config)

        assert isinstance(result, CompressionResult)
        assert len(result.feature_vector) > 0
        assert len(result.feature_vector) <= default_config.max_vector_dimensions
        assert result.original_token_count == sample_jpeg_image.size_bytes // 4

    @pytest.mark.asyncio
    async def test_compress_unsupported_format_passthrough(
        self, compressor, unsupported_image, default_config
    ):
        """Unsupported format should passthrough with empty feature_vector."""
        result = await compressor.compress(unsupported_image, default_config)

        assert result.feature_vector == []
        assert result.compression_ratio == 1.0
        assert result.original_token_count == unsupported_image.size_bytes // 4
        assert result.compressed_token_count == result.original_token_count

    @pytest.mark.asyncio
    async def test_compress_no_mime_type_passthrough(self, compressor, default_config):
        """Image without mime_type should passthrough."""
        image = MediaContent(
            media_type=MediaType.IMAGE,
            mime_type=None,
            size_bytes=4096,
            raw_data=b"\x00" * 4096,
        )
        result = await compressor.compress(image, default_config)

        assert result.feature_vector == []
        assert result.compression_ratio == 1.0

    @pytest.mark.asyncio
    async def test_compress_oversized_passthrough(
        self, compressor, oversized_image, default_config
    ):
        """Oversized image should passthrough."""
        result = await compressor.compress(oversized_image, default_config)

        assert result.feature_vector == []
        assert result.compression_ratio == 1.0
        assert result.original_token_count == oversized_image.size_bytes // 4

    @pytest.mark.asyncio
    async def test_compress_timeout_passthrough(self, compressor, sample_png_image):
        """Timeout should result in passthrough."""
        config = TokenCompressorConfig(
            timeout_seconds=0.001,  # Very short timeout
        )
        # Patch _do_compress to simulate slow processing
        original_do_compress = compressor._do_compress

        async def slow_compress(*args, **kwargs):
            await asyncio.sleep(1.0)
            return await original_do_compress(*args, **kwargs)

        compressor._do_compress = slow_compress

        result = await compressor.compress(sample_png_image, config)

        assert result.feature_vector == []
        assert result.compression_ratio == 1.0

    @pytest.mark.asyncio
    async def test_token_count_calculation(self, compressor, sample_png_image, default_config):
        """Token counts should follow the formula: original=size/4, compressed=dimensions."""
        result = await compressor.compress(sample_png_image, default_config)

        assert result.original_token_count == sample_png_image.size_bytes // 4
        assert result.compressed_token_count == len(result.feature_vector)

    @pytest.mark.asyncio
    async def test_feature_vector_max_dimensions(self, compressor, default_config):
        """Feature vector should never exceed max_vector_dimensions."""
        # Create a very large image to test dimension capping
        large_image = MediaContent(
            media_type=MediaType.IMAGE,
            mime_type="image/png",
            size_bytes=10 * 1024 * 1024,  # 10 MB → 2.5M tokens
            raw_data=b"\x89PNG" + b"\xab" * (10 * 1024 * 1024 - 4),
        )
        result = await compressor.compress(large_image, default_config)

        assert len(result.feature_vector) <= default_config.max_vector_dimensions

    @pytest.mark.asyncio
    async def test_deterministic_output(self, compressor, sample_png_image, default_config):
        """Same input should produce same output (deterministic)."""
        result1 = await compressor.compress(sample_png_image, default_config)
        result2 = await compressor.compress(sample_png_image, default_config)

        assert result1.feature_vector == result2.feature_vector
        assert result1.original_token_count == result2.original_token_count
        assert result1.compressed_token_count == result2.compressed_token_count

    @pytest.mark.asyncio
    async def test_compression_ratio_configurable(self, sample_png_image):
        """Different compression ratios should produce different vector sizes."""
        config_low = TokenCompressorConfig(target_compression_ratio=0.2)
        config_high = TokenCompressorConfig(target_compression_ratio=0.9)

        compressor_low = TokenCompressorStrategy(config=config_low)
        compressor_high = TokenCompressorStrategy(config=config_high)

        result_low = await compressor_low.compress(sample_png_image, config_low)
        result_high = await compressor_high.compress(sample_png_image, config_high)

        # Higher compression ratio → fewer dimensions
        assert len(result_high.feature_vector) <= len(result_low.feature_vector)

    @pytest.mark.asyncio
    async def test_webp_format_supported(self, compressor, default_config):
        """WebP format should be supported."""
        image = MediaContent(
            media_type=MediaType.IMAGE,
            mime_type="image/webp",
            size_bytes=4096,
            raw_data=b"RIFF" + b"\x00" * 4092,
        )
        result = await compressor.compress(image, default_config)

        assert len(result.feature_vector) > 0
        assert result.compression_ratio < 1.0

    @pytest.mark.asyncio
    async def test_bmp_format_supported(self, compressor, default_config):
        """BMP format should be supported."""
        image = MediaContent(
            media_type=MediaType.IMAGE,
            mime_type="image/bmp",
            size_bytes=4096,
            raw_data=b"BM" + b"\x00" * 4094,
        )
        result = await compressor.compress(image, default_config)

        assert len(result.feature_vector) > 0
        assert result.compression_ratio < 1.0


class TestTokenCompressorBatch:
    """Tests for the compress_batch() method."""

    @pytest.mark.asyncio
    async def test_batch_within_limit(self, compressor, sample_png_image, default_config):
        """Batch within max_images_per_request should succeed."""
        images = [sample_png_image] * 3
        results = await compressor.compress_batch(images, default_config)

        assert len(results) == 3
        for result in results:
            assert isinstance(result, CompressionResult)
            assert len(result.feature_vector) > 0

    @pytest.mark.asyncio
    async def test_batch_exceeds_limit(self, compressor, sample_png_image, default_config):
        """Batch exceeding max_images_per_request should raise error."""
        images = [sample_png_image] * 11  # exceeds default limit of 10
        with pytest.raises(TokenCompressionError, match="exceeds maximum"):
            await compressor.compress_batch(images, default_config)

    @pytest.mark.asyncio
    async def test_batch_mixed_formats(self, compressor, default_config):
        """Batch with mixed formats: supported get compressed, unsupported passthrough."""
        supported = MediaContent(
            media_type=MediaType.IMAGE,
            mime_type="image/png",
            size_bytes=4096,
            raw_data=b"\x89PNG" + b"\x00" * 4092,
        )
        unsupported = MediaContent(
            media_type=MediaType.IMAGE,
            mime_type="image/tiff",
            size_bytes=4096,
            raw_data=b"\x49\x49" + b"\x00" * 4094,
        )

        results = await compressor.compress_batch(
            [supported, unsupported], default_config
        )

        assert len(results) == 2
        # First result: compressed
        assert len(results[0].feature_vector) > 0
        assert results[0].compression_ratio < 1.0
        # Second result: passthrough
        assert results[1].feature_vector == []
        assert results[1].compression_ratio == 1.0

    @pytest.mark.asyncio
    async def test_batch_empty_list(self, compressor, default_config):
        """Empty batch should return empty results."""
        results = await compressor.compress_batch([], default_config)
        assert results == []

    @pytest.mark.asyncio
    async def test_batch_exactly_at_limit(self, compressor, sample_png_image, default_config):
        """Batch at exactly max_images_per_request should succeed."""
        images = [sample_png_image] * 10  # exactly at limit
        results = await compressor.compress_batch(images, default_config)
        assert len(results) == 10


class TestTokenCompressorHelpers:
    """Tests for helper methods."""

    def test_is_format_supported_valid(self, compressor, default_config):
        """Supported formats should return True."""
        for fmt in ["image/png", "image/jpeg", "image/webp", "image/bmp"]:
            image = MediaContent(
                media_type=MediaType.IMAGE, mime_type=fmt, size_bytes=100
            )
            assert compressor._is_format_supported(image, default_config) is True

    def test_is_format_supported_case_insensitive(self, compressor, default_config):
        """Format check should be case-insensitive."""
        image = MediaContent(
            media_type=MediaType.IMAGE, mime_type="IMAGE/PNG", size_bytes=100
        )
        assert compressor._is_format_supported(image, default_config) is True

    def test_is_format_supported_invalid(self, compressor, default_config):
        """Unsupported formats should return False."""
        for fmt in ["image/tiff", "image/gif", "image/svg+xml", "video/mp4"]:
            image = MediaContent(
                media_type=MediaType.IMAGE, mime_type=fmt, size_bytes=100
            )
            assert compressor._is_format_supported(image, default_config) is False

    def test_is_format_supported_none_mime(self, compressor, default_config):
        """None mime_type should return False."""
        image = MediaContent(
            media_type=MediaType.IMAGE, mime_type=None, size_bytes=100
        )
        assert compressor._is_format_supported(image, default_config) is False
