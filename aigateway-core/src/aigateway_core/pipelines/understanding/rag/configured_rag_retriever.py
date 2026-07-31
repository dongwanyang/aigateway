"""RAG retriever with explicit infrastructure configuration.

This adapter removes the process-environment configuration channel from plugin
registration. The Qdrant URL is carried by ``RAGRetrieverConfig`` and consumed
by the plugin instance directly.
"""
from __future__ import annotations

import logging

from aigateway_core.pipelines.understanding.rag.rag_retriever_plugin import (
    RAGRetrieverPlugin,
)

logger = logging.getLogger(__name__)


class ConfiguredRAGRetrieverPlugin(RAGRetrieverPlugin):
    """RAG plugin that consumes the Qdrant URL from its config object."""

    def _qdrant_url(self) -> str:
        url = str(getattr(self._config, "qdrant_url", "") or "").strip()
        if not url:
            raise RuntimeError("config_missing:infrastructure.qdrant.url")
        return url.rstrip("/")

    def _initialize_index(self) -> None:
        """Initialize LlamaIndex using the explicitly configured Qdrant URL."""
        try:
            from llama_index.core import VectorStoreIndex
            from llama_index.vector_stores.qdrant import QdrantVectorStore
            from qdrant_client import QdrantClient

            try:
                from qdrant_client import AsyncQdrantClient
            except ImportError:
                AsyncQdrantClient = None

            qdrant_url = self._qdrant_url()
            client = QdrantClient(url=qdrant_url)
            aclient = (
                AsyncQdrantClient(url=qdrant_url)
                if AsyncQdrantClient is not None
                else None
            )

            vector_store_kwargs: dict = {
                "client": client,
                "collection_name": self._config.collection_name,
            }
            if aclient is not None:
                vector_store_kwargs["aclient"] = aclient

            vector_store = QdrantVectorStore(**vector_store_kwargs)
            embed_model = self._resolve_embed_model()
            index_kwargs: dict = {"vector_store": vector_store}
            if embed_model is not None:
                index_kwargs["embed_model"] = embed_model

            self._index = VectorStoreIndex.from_vector_store(**index_kwargs)
            self._is_available = True
            logger.info(
                "RAGRetrieverPlugin initialized: collection=%s, qdrant_url=%s, "
                "top_k=%d, embedding_backend=%s",
                self._config.collection_name,
                qdrant_url,
                self._config.top_k,
                getattr(self._config, "embedding_backend", "local"),
            )
        except ImportError:
            self._is_available = False
            logger.warning(
                "llama_index or qdrant_client is unavailable; RAG retrieval will "
                "run in passthrough mode"
            )
        except Exception as exc:
            self._is_available = False
            logger.warning(
                "RAGRetrieverPlugin initialization failed; passthrough mode: %s",
                exc,
            )


__all__ = ["ConfiguredRAGRetrieverPlugin"]
