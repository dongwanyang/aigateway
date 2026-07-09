"""
Unit tests for RAGRetrieverPlugin.ingest_documents().

Validates:
- Document ingestion with string inputs
- Document ingestion with Document objects
- Unavailable mode returns proper status
- Exception handling returns error status
- Chunk size and overlap configuration is respected

需求: 5.9
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.shared.integration_configs import RAGRetrieverConfig


class TestIngestDocumentsUnavailable:
    """Test ingest_documents when llama_index is not available."""

    @patch(
        "aigateway_core.plugins.rag_retriever_plugin.RAGRetrieverPlugin._initialize_index"
    )
    def test_returns_unavailable_when_not_installed(self, mock_init):
        """When _is_available is False, ingest should return unavailable status."""
        from aigateway_core.plugins.rag_retriever_plugin import RAGRetrieverPlugin

        plugin = RAGRetrieverPlugin.__new__(RAGRetrieverPlugin)
        plugin._config = RAGRetrieverConfig()
        plugin._is_available = False
        plugin._index = None

        import asyncio

        result = asyncio.run(plugin.ingest_documents(["hello world"]))
        assert result["status"] == "unavailable"
        assert "llama_index not installed" in result["reason"]


class TestIngestDocumentsWithMocks:
    """Test ingest_documents with mocked LlamaIndex dependencies."""

    @patch(
        "aigateway_core.plugins.rag_retriever_plugin.RAGRetrieverPlugin._initialize_index"
    )
    def test_ingest_string_documents(self, mock_init):
        """String documents should be converted to Document objects and ingested."""
        from aigateway_core.plugins.rag_retriever_plugin import RAGRetrieverPlugin

        plugin = RAGRetrieverPlugin.__new__(RAGRetrieverPlugin)
        plugin._config = RAGRetrieverConfig(chunk_size=512, chunk_overlap=64)
        plugin._is_available = True
        plugin._index = MagicMock()

        mock_document_cls = MagicMock()
        mock_splitter_cls = MagicMock()
        mock_splitter_instance = MagicMock()
        mock_splitter_cls.return_value = mock_splitter_instance
        # Simulate 3 nodes from 2 documents
        mock_splitter_instance.get_nodes_from_documents.return_value = [
            MagicMock(),
            MagicMock(),
            MagicMock(),
        ]

        with patch.dict(
            "sys.modules",
            {
                "llama_index": MagicMock(),
                "llama_index.core": MagicMock(Document=mock_document_cls),
                "llama_index.core.node_parser": MagicMock(
                    SentenceSplitter=mock_splitter_cls
                ),
            },
        ):
            # Need to reimport to pick up mocked modules
            import importlib
            import aigateway_core.plugins.rag_retriever_plugin as mod

            importlib.reload(mod)

            plugin_reloaded = mod.RAGRetrieverPlugin.__new__(mod.RAGRetrieverPlugin)
            plugin_reloaded._config = RAGRetrieverConfig(
                chunk_size=256, chunk_overlap=32
            )
            plugin_reloaded._is_available = True
            plugin_reloaded._index = MagicMock()

            import asyncio

            result = asyncio.run(
                plugin_reloaded.ingest_documents(["doc1 text", "doc2 text"])
            )

            assert result["status"] == "success"
            assert result["num_documents"] == 2
            assert result["num_chunks"] == 3

            # Verify splitter was created with correct config
            mock_splitter_cls.assert_called_once_with(chunk_size=256, chunk_overlap=32)

            # Verify insert_nodes was called
            plugin_reloaded._index.insert_nodes.assert_called_once()

    @patch(
        "aigateway_core.plugins.rag_retriever_plugin.RAGRetrieverPlugin._initialize_index"
    )
    def test_ingest_returns_error_on_exception(self, mock_init):
        """When an exception occurs during ingest, should return error status."""
        from aigateway_core.plugins.rag_retriever_plugin import RAGRetrieverPlugin

        plugin = RAGRetrieverPlugin.__new__(RAGRetrieverPlugin)
        plugin._config = RAGRetrieverConfig()
        plugin._is_available = True
        plugin._index = MagicMock()
        plugin._index.insert_nodes.side_effect = RuntimeError("Qdrant connection failed")

        mock_document_cls = MagicMock()
        mock_splitter_cls = MagicMock()
        mock_splitter_instance = MagicMock()
        mock_splitter_cls.return_value = mock_splitter_instance
        mock_splitter_instance.get_nodes_from_documents.return_value = [MagicMock()]

        with patch.dict(
            "sys.modules",
            {
                "llama_index": MagicMock(),
                "llama_index.core": MagicMock(Document=mock_document_cls),
                "llama_index.core.node_parser": MagicMock(
                    SentenceSplitter=mock_splitter_cls
                ),
            },
        ):
            import importlib
            import aigateway_core.plugins.rag_retriever_plugin as mod

            importlib.reload(mod)

            plugin_reloaded = mod.RAGRetrieverPlugin.__new__(mod.RAGRetrieverPlugin)
            plugin_reloaded._config = RAGRetrieverConfig()
            plugin_reloaded._is_available = True
            plugin_reloaded._index = MagicMock()
            plugin_reloaded._index.insert_nodes.side_effect = RuntimeError(
                "Qdrant connection failed"
            )

            import asyncio

            result = asyncio.run(plugin_reloaded.ingest_documents(["some text"]))

            assert result["status"] == "error"
            assert "Qdrant connection failed" in result["reason"]

    @patch(
        "aigateway_core.plugins.rag_retriever_plugin.RAGRetrieverPlugin._initialize_index"
    )
    def test_ingest_empty_documents_list(self, mock_init):
        """Ingesting empty list should return success with zero counts."""
        from aigateway_core.plugins.rag_retriever_plugin import RAGRetrieverPlugin

        plugin = RAGRetrieverPlugin.__new__(RAGRetrieverPlugin)
        plugin._config = RAGRetrieverConfig()
        plugin._is_available = True
        plugin._index = MagicMock()

        mock_document_cls = MagicMock()
        mock_splitter_cls = MagicMock()
        mock_splitter_instance = MagicMock()
        mock_splitter_cls.return_value = mock_splitter_instance
        mock_splitter_instance.get_nodes_from_documents.return_value = []

        with patch.dict(
            "sys.modules",
            {
                "llama_index": MagicMock(),
                "llama_index.core": MagicMock(Document=mock_document_cls),
                "llama_index.core.node_parser": MagicMock(
                    SentenceSplitter=mock_splitter_cls
                ),
            },
        ):
            import importlib
            import aigateway_core.plugins.rag_retriever_plugin as mod

            importlib.reload(mod)

            plugin_reloaded = mod.RAGRetrieverPlugin.__new__(mod.RAGRetrieverPlugin)
            plugin_reloaded._config = RAGRetrieverConfig()
            plugin_reloaded._is_available = True
            plugin_reloaded._index = MagicMock()

            import asyncio

            result = asyncio.run(plugin_reloaded.ingest_documents([]))

            assert result["status"] == "success"
            assert result["num_documents"] == 0
            assert result["num_chunks"] == 0
