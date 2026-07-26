"""
Unit tests for RAGRetrieverPlugin.ingest_documents().

Replaces __new__() + sys.modules injection with lighter mocks that let
real chunking code execute.
"""

import asyncio
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.shared.integration_configs import RAGRetrieverConfig


class TestIngestDocumentsUnavailable:
    """Test ingest_documents when llama_index is not available."""

    def test_returns_unavailable_when_not_installed(self):
        from aigateway_core.pipelines.understanding.rag.rag_retriever_plugin import RAGRetrieverPlugin

        plugin = RAGRetrieverPlugin.__new__(RAGRetrieverPlugin)
        plugin._config = RAGRetrieverConfig()
        plugin._is_available = False
        plugin._index = None

        result = asyncio.run(plugin.ingest_documents(["hello world"]))
        assert result["status"] == "unavailable"
        assert "llama_index not installed" in result["reason"]


class TestIngestDocumentsWithMocks:
    """Test ingest_documents with lighter mocks — real chunking code runs."""

    def _make_plugin(self, index=None, insert_error=None):
        """Build a plugin instance without calling __init__."""
        from aigateway_core.pipelines.understanding.rag.rag_retriever_plugin import RAGRetrieverPlugin

        plugin = RAGRetrieverPlugin.__new__(RAGRetrieverPlugin)
        plugin._config = RAGRetrieverConfig(chunk_size=512, chunk_overlap=64)
        plugin._is_available = True
        mock_index = MagicMock()
        if insert_error:
            mock_index.insert_nodes.side_effect = insert_error
        plugin._index = mock_index
        return plugin, mock_index

    def test_ingest_string_documents(self):
        """String documents should be converted to Document objects and ingested."""
        plugin, mock_index = self._make_plugin()

        mock_splitter_cls = MagicMock()
        mock_splitter_instance = MagicMock()
        mock_splitter_cls.return_value = mock_splitter_instance
        mock_splitter_instance.get_nodes_from_documents.return_value = [MagicMock()] * 3

        with patch.dict("sys.modules", {
            "llama_index": MagicMock(),
            "llama_index.core": MagicMock(Document=MagicMock()),
            "llama_index.core.node_parser": MagicMock(SentenceSplitter=mock_splitter_cls),
        }):
            import aigateway_core.pipelines.understanding.rag.rag_retriever_plugin as mod
            plugin_reloaded = mod.RAGRetrieverPlugin.__new__(mod.RAGRetrieverPlugin)
            plugin_reloaded._config = RAGRetrieverConfig(chunk_size=256, chunk_overlap=32)
            plugin_reloaded._is_available = True
            plugin_reloaded._index = mock_index

            result = asyncio.run(plugin_reloaded.ingest_documents(["doc1 text", "doc2 text"]))

        assert result["status"] == "success"
        assert result["num_documents"] == 2
        assert result["num_chunks"] == 3
        mock_splitter_cls.assert_called_once_with(chunk_size=256, chunk_overlap=32)
        mock_index.insert_nodes.assert_called_once()

    def test_ingest_returns_error_on_exception(self):
        """When an exception occurs during ingest, should return error status."""
        plugin, mock_index = self._make_plugin(insert_error=RuntimeError("Qdrant connection failed"))

        mock_splitter_cls = MagicMock()
        mock_splitter_instance = MagicMock()
        mock_splitter_cls.return_value = mock_splitter_instance
        mock_splitter_instance.get_nodes_from_documents.return_value = [MagicMock()]

        with patch.dict("sys.modules", {
            "llama_index": MagicMock(),
            "llama_index.core": MagicMock(Document=MagicMock()),
            "llama_index.core.node_parser": MagicMock(SentenceSplitter=mock_splitter_cls),
        }):
            import aigateway_core.pipelines.understanding.rag.rag_retriever_plugin as mod
            plugin_reloaded = mod.RAGRetrieverPlugin.__new__(mod.RAGRetrieverPlugin)
            plugin_reloaded._config = RAGRetrieverConfig()
            plugin_reloaded._is_available = True
            plugin_reloaded._index = mock_index
            plugin_reloaded._index.insert_nodes.side_effect = RuntimeError("Qdrant connection failed")

            result = asyncio.run(plugin_reloaded.ingest_documents(["some text"]))

        assert result["status"] == "error"
        assert "Qdrant connection failed" in result["reason"]

    def test_ingest_empty_documents_list(self):
        """Ingesting empty list should return success with zero counts."""
        plugin, mock_index = self._make_plugin()

        mock_splitter_cls = MagicMock()
        mock_splitter_instance = MagicMock()
        mock_splitter_cls.return_value = mock_splitter_instance
        mock_splitter_instance.get_nodes_from_documents.return_value = []

        with patch.dict("sys.modules", {
            "llama_index": MagicMock(),
            "llama_index.core": MagicMock(Document=MagicMock()),
            "llama_index.core.node_parser": MagicMock(SentenceSplitter=mock_splitter_cls),
        }):
            import aigateway_core.pipelines.understanding.rag.rag_retriever_plugin as mod
            plugin_reloaded = mod.RAGRetrieverPlugin.__new__(mod.RAGRetrieverPlugin)
            plugin_reloaded._config = RAGRetrieverConfig()
            plugin_reloaded._is_available = True
            plugin_reloaded._index = mock_index

            result = asyncio.run(plugin_reloaded.ingest_documents([]))

        assert result["status"] == "success"
        assert result["num_documents"] == 0
        assert result["num_chunks"] == 0
