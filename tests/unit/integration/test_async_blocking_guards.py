"""Regression tests for synchronous work accidentally running on the event loop."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aigateway_api import openai_compat
from aigateway_core.pipelines.generation._common.config import TokenCompressorConfig
from aigateway_core.pipelines.generation._common.models import CompressionResult
from aigateway_core.pipelines.generation.token.token_compressor import (
    TokenCompressorStrategy,
)
from aigateway_core.pipelines.generation.token import token_compressor
from aigateway_core.pipelines.understanding.rag.rag_retriever_plugin import (
    RAGRetrieverPlugin,
)
from aigateway_core.pipelines.understanding.rag import rag_retriever_plugin
from aigateway_core.prefix.cache import l3_semantic
from aigateway_core.prefix.cache import plugin as cache_plugin
from aigateway_core.prefix.cache.plugin import SemanticCachePlugin
from aigateway_core.prefix.media.types import MediaContent, MediaType


@pytest.mark.asyncio
async def test_openai_local_embeddings_are_offloaded():
    def encode(_model, _inputs):
        return [[0.1, 0.2]]

    async def run_sync(func, *args, **_kwargs):
        return func(*args)

    offloader = AsyncMock(side_effect=run_sync)
    body = openai_compat.EmbeddingRequest(model="test-model", input="hello")
    config_manager = MagicMock()
    config_manager.get.return_value = {"backend": "sentence_transformers"}

    with (
        patch.object(
            openai_compat,
            "_get_app_state",
            return_value={"config_manager": config_manager},
        ),
        patch.object(
            openai_compat,
            "_encode_with_sentence_transformer",
            side_effect=encode,
        ),
        patch.object(openai_compat.asyncio, "to_thread", offloader),
    ):
        response = await openai_compat.create_embeddings(body, MagicMock())

    assert response.status_code == 200
    offloader.assert_awaited_once()


@pytest.mark.asyncio
async def test_semantic_cache_embedding_is_offloaded():
    plugin = SemanticCachePlugin()

    def compute(_text):
        return [0.1, 0.2]

    async def run_sync(func, *args, **_kwargs):
        return func(*args)

    offloader = AsyncMock(side_effect=run_sync)
    with patch.object(
        plugin, "_compute_embedding_sync", side_effect=compute
    ), patch.object(
        cache_plugin.asyncio, "to_thread", offloader
    ):
        result = await plugin._compute_embedding("hello")

    assert result == [0.1, 0.2]
    offloader.assert_awaited_once()


@pytest.mark.asyncio
async def test_l3_embedding_is_offloaded():
    def compute(_text, _load_if_missing):
        return [0.1, 0.2]

    async def run_sync(func, *args, **_kwargs):
        return func(*args)

    offloader = AsyncMock(side_effect=run_sync)
    with patch.object(
        l3_semantic, "_compute_l3_vector_sync", side_effect=compute
    ), patch.object(
        l3_semantic.asyncio, "to_thread", offloader
    ):
        result = await l3_semantic._compute_l3_vector("hello")

    assert result == [0.1, 0.2]
    offloader.assert_awaited_once()


@pytest.mark.asyncio
async def test_token_compression_is_offloaded():
    config = TokenCompressorConfig(timeout_seconds=1.0)
    strategy = TokenCompressorStrategy(config)
    image = MediaContent(
        media_type=MediaType.IMAGE,
        mime_type="image/png",
        size_bytes=16,
        raw_data=b"\x89PNG" + b"\x00" * 12,
    )
    expected = CompressionResult(
        feature_vector=[0.1],
        original_token_count=4,
        compressed_token_count=1,
        compression_ratio=0.75,
    )

    def compress(_image, _config):
        return expected

    async def run_sync(func, *args, **_kwargs):
        return func(*args)

    offloader = AsyncMock(side_effect=run_sync)
    with patch.object(
        strategy, "_do_compress_sync", side_effect=compress
    ), patch.object(
        token_compressor.asyncio, "to_thread", offloader
    ):
        result = await strategy.compress(image, config)

    assert result.feature_vector == [0.1]
    offloader.assert_awaited_once()


@pytest.mark.asyncio
async def test_reranker_is_offloaded():
    plugin = RAGRetrieverPlugin()
    nodes = [object()]

    def rerank(_query, values):
        return values

    async def run_sync(func, *args, **_kwargs):
        return func(*args)

    offloader = AsyncMock(side_effect=run_sync)
    with patch.object(
        plugin, "_rerank_sync", side_effect=rerank
    ), patch.object(
        rag_retriever_plugin.asyncio, "to_thread", offloader
    ):
        result = await plugin._rerank("hello", nodes)

    assert result == nodes
    offloader.assert_awaited_once()
