from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aigateway_core.pipelines.understanding.rag.configured_rag_retriever import (
    ConfiguredRAGRetrieverPlugin,
)
from aigateway_core.shared.integration_configs import RAGRetrieverConfig
from aigateway_core.shared.qdrant_client import QdrantClientManager


@pytest.mark.asyncio
async def test_manager_applies_api_key_to_default_http_headers(monkeypatch) -> None:
    monkeypatch.setenv("QDRANT_API_KEY", "protected-qdrant-key")
    response = MagicMock()
    response.raise_for_status = MagicMock()
    http = AsyncMock()
    http.get = AsyncMock(return_value=response)
    http.aclose = AsyncMock()

    with patch(
        "aigateway_core.shared.qdrant_client.AsyncClient",
        return_value=http,
    ) as client_factory:
        manager = QdrantClientManager()
        await manager.connect(
            "https://qdrant.example",
            connect_timeout=1,
            read_timeout=2,
            write_timeout=3,
        )

    assert client_factory.call_args.kwargs["headers"]["api-key"] == (
        "protected-qdrant-key"
    )
    http.get.assert_awaited_once_with("/")


def test_rag_official_clients_receive_qdrant_api_key(monkeypatch) -> None:
    monkeypatch.setenv("QDRANT_API_KEY", "protected-qdrant-key")
    sync_client = MagicMock(name="QdrantClient")
    async_client = MagicMock(name="AsyncQdrantClient")

    llama_package = ModuleType("llama_index")
    core_module = ModuleType("llama_index.core")
    vector_package = ModuleType("llama_index.vector_stores")
    vector_module = ModuleType("llama_index.vector_stores.qdrant")
    qdrant_module = ModuleType("qdrant_client")

    class FakeVectorStoreIndex:
        @classmethod
        def from_vector_store(cls, **_kwargs):
            return object()

    core_module.VectorStoreIndex = FakeVectorStoreIndex
    vector_module.QdrantVectorStore = MagicMock(return_value=object())
    qdrant_module.QdrantClient = sync_client
    qdrant_module.AsyncQdrantClient = async_client

    modules = {
        "llama_index": llama_package,
        "llama_index.core": core_module,
        "llama_index.vector_stores": vector_package,
        "llama_index.vector_stores.qdrant": vector_module,
        "qdrant_client": qdrant_module,
    }
    with patch.dict(sys.modules, modules):
        plugin = ConfiguredRAGRetrieverPlugin(
            RAGRetrieverConfig(
                collection_name="documents",
                qdrant_url="https://qdrant.example",
            )
        )
        plugin._resolve_embed_model = lambda: None
        plugin._initialize_index()

    expected = {
        "url": "https://qdrant.example",
        "api_key": "protected-qdrant-key",
    }
    sync_client.assert_called_once_with(**expected)
    async_client.assert_called_once_with(**expected)
    assert plugin._is_available is True
