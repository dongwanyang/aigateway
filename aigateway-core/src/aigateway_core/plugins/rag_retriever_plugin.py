"""
RAGRetrieverPlugin — LlamaIndex + Qdrant RAG 检索插件
=====================================================

在理解型管道中从 Qdrant 向量数据库检索与用户查询相关的文档上下文，
增强 LLM 的回答准确性。

依赖: llama-index, llama-index-vector-stores-qdrant (可选依赖)
当 llama_index 未安装时，以 passthrough 模式运行。

需求: 5.1, 5.2, 5.8
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..context import NS_RAG_RETRIEVER, PipelineContext
from ..integration_configs import RAGRetrieverConfig

logger = logging.getLogger(__name__)


class RAGRetrieverPlugin:
    """RAG 检索插件 — LlamaIndex VectorStoreIndex + Qdrant。

    从 Qdrant 向量数据库中检索与用户查询最相关的文档块，
    并将结果注入 PipelineContext 供下游插件（如 PromptCompressPlugin）消费。

    Attributes:
        name: 插件名称。
        enabled: 是否启用。
        depends_on: 依赖的前置插件列表。
    """

    name: str = "rag_retriever"
    enabled: bool = True
    depends_on: list = ["semantic_cache"]

    def __init__(self, config: Optional[RAGRetrieverConfig] = None) -> None:
        """初始化 RAG 检索插件。

        Args:
            config: RAG 检索配置，若为 None 则使用默认配置。
        """
        self._config = config or RAGRetrieverConfig()
        self._is_available: bool = False
        self._index: Any = None  # VectorStoreIndex instance
        self._initialize_index()

    def _initialize_index(self) -> None:
        """初始化 LlamaIndex VectorStoreIndex + QdrantVectorStore。

        尝试导入 llama_index 并连接到 Qdrant。
        若 ImportError 或初始化异常，标记为不可用并以 passthrough 模式运行。
        """
        try:
            from llama_index.core import VectorStoreIndex
            from llama_index.vector_stores.qdrant import QdrantVectorStore

            import os

            qdrant_url = os.environ.get("QDRANT_URL", "http://localhost:6333")

            # 使用 qdrant_client 库创建客户端连接
            from qdrant_client import QdrantClient

            client = QdrantClient(url=qdrant_url)

            # 创建 QdrantVectorStore
            vector_store = QdrantVectorStore(
                client=client,
                collection_name=self._config.collection_name,
            )

            # 从已有向量存储创建索引（不重新索引）
            self._index = VectorStoreIndex.from_vector_store(
                vector_store=vector_store,
            )
            self._is_available = True
            logger.info(
                "RAGRetrieverPlugin 已初始化: collection=%s, qdrant_url=%s, top_k=%d",
                self._config.collection_name,
                qdrant_url,
                self._config.top_k,
            )

        except ImportError:
            self._is_available = False
            logger.warning(
                "llama_index 或 qdrant_client 未安装，RAGRetrieverPlugin 将以 passthrough 模式运行。"
                "安装方式: pip install 'llama-index>=0.10.0' 'llama-index-vector-stores-qdrant>=0.2.0'"
            )
        except Exception as exc:
            self._is_available = False
            logger.warning(
                "RAGRetrieverPlugin 初始化失败，降级为 passthrough: %s", exc
            )

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        """执行 RAG 检索。

        1. 若不可用，直接返回 ctx（passthrough）
        2. 从 ctx.request["messages"] 提取最新用户消息作为查询
        3. 使用 VectorStoreIndex.as_retriever() 检索 top_k 文档块
        4. 按 similarity_threshold 过滤结果
        5. 可选: rerank 步骤（rerank_enabled=True 时）
        6. 将检索结果存储到 ctx.extra["rag_retriever"]["retrieved_chunks"]
        7. 将检索到的上下文注入为 system message 前缀
        8. 异常时 log warning 并 passthrough

        Args:
            ctx: 管线上下文。

        Returns:
            更新后的上下文（含检索结果）或原始上下文（passthrough）。
        """
        if not self._is_available:
            return ctx

        # 提取用户查询：取最后一条 role=user 的消息内容
        messages = ctx.request.get("messages", [])
        user_query = self._extract_user_query(messages)
        if not user_query:
            return ctx

        try:
            # 使用 LlamaIndex retriever 检索
            retriever = self._index.as_retriever(
                similarity_top_k=self._config.top_k,
            )
            nodes = await retriever.aretrieve(user_query)

            # 过滤低于相似度阈值的结果
            filtered_nodes = []
            for node in nodes:
                score = getattr(node, "score", None)
                if score is not None and score < self._config.similarity_threshold:
                    continue
                filtered_nodes.append(node)

            # 可选 rerank 步骤
            if self._config.rerank_enabled and filtered_nodes:
                filtered_nodes = await self._rerank(user_query, filtered_nodes)

            # 提取文本内容
            retrieved_chunks: List[str] = []
            for node in filtered_nodes:
                text = node.get_content() if hasattr(node, "get_content") else str(node)
                if text.strip():
                    retrieved_chunks.append(text)

            # 写入 ctx.extra["rag_retriever"]["retrieved_chunks"]
            if NS_RAG_RETRIEVER not in ctx.extra:
                ctx.extra[NS_RAG_RETRIEVER] = {}
            ctx.extra[NS_RAG_RETRIEVER]["retrieved_chunks"] = retrieved_chunks
            ctx.extra[NS_RAG_RETRIEVER]["query"] = user_query
            ctx.extra[NS_RAG_RETRIEVER]["top_k"] = self._config.top_k
            ctx.extra[NS_RAG_RETRIEVER]["num_results"] = len(retrieved_chunks)

            # 注入为 system message 前缀
            if retrieved_chunks:
                self._inject_system_message(ctx, retrieved_chunks)
                logger.debug(
                    "RAG 检索完成: query=%s, num_results=%d, request_id=%s",
                    user_query[:50],
                    len(retrieved_chunks),
                    ctx.request_id,
                )

        except Exception as exc:
            logger.warning(
                "RAGRetrieverPlugin 检索异常，降级为 passthrough: %s, request_id=%s",
                exc,
                ctx.request_id,
            )
            # 确保命名空间存在但结果为空
            if NS_RAG_RETRIEVER not in ctx.extra:
                ctx.extra[NS_RAG_RETRIEVER] = {}
            ctx.extra[NS_RAG_RETRIEVER].setdefault("retrieved_chunks", [])

        return ctx

    async def _rerank(self, query: str, nodes: List[Any]) -> List[Any]:
        """对检索结果进行重排序以提高精度。

        当前为基础实现 — 尝试使用 cross-encoder 模型进行重排序。
        若 rerank 模型不可用，则返回原始顺序。

        Args:
            query: 用户查询文本。
            nodes: 检索到的节点列表。

        Returns:
            重排序后的节点列表。
        """
        try:
            from llama_index.core.postprocessor import SentenceTransformerRerank

            reranker = SentenceTransformerRerank(
                model=self._config.rerank_model,
                top_n=len(nodes),
            )
            # SentenceTransformerRerank 需要 QueryBundle
            from llama_index.core.schema import QueryBundle

            query_bundle = QueryBundle(query_str=query)
            reranked = reranker.postprocess_nodes(nodes, query_bundle=query_bundle)
            return reranked
        except ImportError:
            logger.warning(
                "Rerank 依赖未安装（需要 sentence-transformers），跳过重排序步骤。"
            )
            return nodes
        except Exception as exc:
            logger.warning(
                "Rerank 执行失败，返回原始排序: %s", exc
            )
            return nodes

    def _inject_system_message(self, ctx: PipelineContext, chunks: List[str]) -> None:
        """将检索到的文档块注入为 system message 前缀。

        在 messages 列表开头插入或更新一条 system 消息，
        包含 RAG 检索到的参考上下文。

        Args:
            ctx: 管线上下文。
            chunks: 检索到的文档块列表。
        """
        rag_context_text = "\n\n---\n\n".join(chunks)
        rag_system_content = (
            f"[参考上下文 - 以下内容来自知识库检索结果，请基于这些信息回答用户问题]\n\n"
            f"{rag_context_text}\n\n"
            f"[参考上下文结束]"
        )

        messages = ctx.request.get("messages", [])
        if not messages:
            return

        # 如果第一条消息是 system 消息，在其 content 前追加 RAG 上下文
        if messages[0].get("role") == "system":
            original_system = messages[0].get("content", "")
            messages[0]["content"] = f"{rag_system_content}\n\n{original_system}"
        else:
            # 在消息列表最前面插入一条 RAG system 消息
            messages.insert(0, {"role": "system", "content": rag_system_content})

    async def ingest_documents(self, documents: list) -> dict:
        """文档加载、分块、embedding 生成、索引 upsert。

        Args:
            documents: LlamaIndex Document 列表或文本字符串列表

        Returns:
            {"status": "success", "num_documents": N, "num_chunks": M}
        """
        if not self._is_available:
            return {"status": "unavailable", "reason": "llama_index not installed"}

        try:
            from llama_index.core import Document
            from llama_index.core.node_parser import SentenceSplitter

            # Convert strings to Document objects if needed
            docs = []
            for doc in documents:
                if isinstance(doc, str):
                    docs.append(Document(text=doc))
                else:
                    docs.append(doc)

            # Chunk documents
            splitter = SentenceSplitter(
                chunk_size=self._config.chunk_size,
                chunk_overlap=self._config.chunk_overlap,
            )
            nodes = splitter.get_nodes_from_documents(docs)

            # Insert into index
            self._index.insert_nodes(nodes)

            return {
                "status": "success",
                "num_documents": len(docs),
                "num_chunks": len(nodes),
            }
        except Exception as exc:
            logger.warning("文档 Ingest 失败: %s", exc)
            return {"status": "error", "reason": str(exc)}

    def _extract_user_query(self, messages: List[Dict[str, Any]]) -> str:
        """从消息列表中提取最后一条用户消息内容作为查询。

        Args:
            messages: OpenAI 格式的消息列表。

        Returns:
            最后一条用户消息的文本内容，若无则返回空字符串。
        """
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content.strip()
                elif isinstance(content, list):
                    # 多模态消息：提取 text 类型部分
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                    return " ".join(text_parts).strip()
        return ""
