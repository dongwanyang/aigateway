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

import asyncio
import logging
import os
from collections import defaultdict
from typing import Any, Dict, List, Optional

from aigateway_core.dispatch.context import NS_RAG_RETRIEVER, PipelineContext
from aigateway_core.shared.integration_configs import RAGRetrieverConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Code RAG helpers (module-level, unit-tested)
# ---------------------------------------------------------------------------


def _filter_code_collections(names: List[str]) -> List[str]:
    """从 Qdrant 集合列表挑出 rag_code_* 代码集合。"""
    return [name for name in names if isinstance(name, str) and name.startswith("rag_code_")]


def _select_code_collections_for_model(
    names: List[str], embedding_model: str
) -> List[str]:
    """从 rag_code_* 集合列表里挑出与当前 embedding_model 匹配的那一份。

    Code RAG 按 embedding_model 分独立集合(维度不同)——检索时若把同一个查询
    向量投到所有集合上,维度不匹配的集合会被 Qdrant 拒 4xx,然后被上层 tolerant
    分支静默吞掉,导致其他嵌入模型的仓库"消失"。这里做前置过滤,只查匹配集合。

    - 空 embedding_model → 返回原列表(避免误伤,交给调用方处理)
    - 找到精确匹配 → 只返回它
    - 找不到任何匹配 → 返回空列表(比全部盲扫更安全)
    """
    # lazy 引用,避免 aigateway_core 顶层 import 时强制拉 sentence-transformers
    from aigateway_core.pipelines.understanding.code_rag.embedding_router import resolve_collection_name

    code_collections = _filter_code_collections(names)
    if not embedding_model:
        return code_collections
    target = resolve_collection_name(embedding_model)
    return [name for name in code_collections if name == target]


def _dedupe_hits_by_identity(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 (document_id, file_path/filename, chunk_index) 去重,保留首次出现。"""
    seen: set[tuple[str, str, int]] = set()
    result: List[Dict[str, Any]] = []
    for item in items:
        try:
            chunk_index = int(item.get("chunk_index", 0) or 0)
        except (TypeError, ValueError):
            chunk_index = 0
        key = (
            str(item.get("document_id", "")),
            str(item.get("file_path", item.get("filename", ""))),
            chunk_index,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _expand_code_hit_metadata(
    hit: Dict[str, Any], graph_metadata: Dict[str, Any]
) -> Dict[str, Any]:
    """把图谱查回的 callers/callees/imports 并入 hit payload."""
    merged = dict(hit)
    merged["callers"] = list(graph_metadata.get("callers", []) or [])
    merged["callees"] = list(graph_metadata.get("callees", []) or [])
    merged["imports"] = list(graph_metadata.get("imports", []) or [])
    return merged


def _code_hit_identity(hit: Dict[str, Any]) -> tuple[str, str, int]:
    try:
        chunk_index = int(hit.get("chunk_index", 0) or 0)
    except (TypeError, ValueError):
        chunk_index = 0
    return (
        str(hit.get("document_id", "")),
        str(hit.get("file_path", hit.get("filename", ""))),
        chunk_index,
    )


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

            # 项目主流使用 AI_GATEWAY_QDRANT_URL 变量，向后兼容裸 QDRANT_URL
            qdrant_url = (
                os.environ.get("AI_GATEWAY_QDRANT_URL")
                or os.environ.get("QDRANT_URL")
                or "http://localhost:6333"
            )

            # 使用 qdrant_client 库创建客户端连接
            from qdrant_client import QdrantClient

            client = QdrantClient(url=qdrant_url)

            # 创建 QdrantVectorStore
            vector_store = QdrantVectorStore(
                client=client,
                collection_name=self._config.collection_name,
            )

            # 选择 embedding 后端：默认本地 HuggingFace 模型，避免依赖 OPENAI_API_KEY
            embed_model = self._resolve_embed_model()

            # 从已有向量存储创建索引（不重新索引）
            index_kwargs: dict = {"vector_store": vector_store}
            if embed_model is not None:
                index_kwargs["embed_model"] = embed_model
            self._index = VectorStoreIndex.from_vector_store(**index_kwargs)
            self._is_available = True
            logger.info(
                "RAGRetrieverPlugin 已初始化: collection=%s, qdrant_url=%s, top_k=%d, embedding_backend=%s",
                self._config.collection_name,
                qdrant_url,
                self._config.top_k,
                getattr(self._config, "embedding_backend", "local"),
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

    def _resolve_embed_model(self) -> Any:
        """按配置返回 LlamaIndex embedding 实例。

        embedding_backend == "local": 用 HuggingFace 本地模型（默认 Qwen3-Embedding-0.6B，
        与 L3 语义缓存一致），无需外部 API Key。
        embedding_backend == "openai": 用 OpenAI 兼容端点，读取 embedding_api_base / embedding_api_key。
        其他值：返回 None，走 LlamaIndex 默认（会强依赖 OPENAI_API_KEY）。

        任一后端初始化失败时返回 None 并记录 warning。
        """
        backend = getattr(self._config, "embedding_backend", "local")
        model_name = getattr(self._config, "embedding_model", "Qwen/Qwen3-Embedding-0.6B")

        if backend == "local":
            try:
                from llama_index.embeddings.huggingface import HuggingFaceEmbedding
                return HuggingFaceEmbedding(
                    model_name=model_name,
                    trust_remote_code=True,
                )
            except ImportError:
                logger.warning(
                    "llama-index-embeddings-huggingface 未安装，"
                    "RAG 将回退到 LlamaIndex 默认 embedding（需 OPENAI_API_KEY）。"
                    "安装: pip install llama-index-embeddings-huggingface"
                )
                return None
            except Exception as exc:
                logger.warning("本地 HuggingFace embedding 加载失败: %s", exc)
                return None

        if backend == "openai":
            api_base = getattr(self._config, "embedding_api_base", None)
            api_key = getattr(self._config, "embedding_api_key", None)
            try:
                from llama_index.embeddings.openai import OpenAIEmbedding
                kwargs: dict = {"model_name": model_name}
                if api_base:
                    kwargs["api_base"] = api_base
                if api_key:
                    kwargs["api_key"] = api_key
                return OpenAIEmbedding(**kwargs)
            except ImportError:
                logger.warning(
                    "llama-index-embeddings-openai 未安装，"
                    "RAG 将回退到 LlamaIndex 默认 embedding。"
                    "安装: pip install llama-index-embeddings-openai"
                )
                return None
            except Exception as exc:
                logger.warning("OpenAI 兼容 embedding 初始化失败: %s", exc)
                return None

        logger.warning(
            "RAGRetrieverConfig.embedding_backend=%r 未识别，回退到 LlamaIndex 默认",
            backend,
        )
        return None

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

            # --- Code RAG: 并行查询 rag_code_* 代码集合并做图谱跳数展开 ---
            # 该分支彻底 "tolerant on retrieval" —— 任何异常都只记 warning,
            # 不影响文本检索主链路。
            code_hits: List[Dict[str, Any]] = []
            if getattr(self._config, "code_rag_enabled", False):
                try:
                    code_hits = await self._retrieve_code_hits(user_query)
                    code_hits = await self._expand_code_hits_with_graph(code_hits)
                    code_hits = _dedupe_hits_by_identity(code_hits)
                    ctx.extra[NS_RAG_RETRIEVER]["code_hits"] = code_hits
                except Exception as code_exc:
                    logger.warning(
                        "Code RAG 检索异常,降级为无代码上下文: %s, request_id=%s",
                        code_exc,
                        ctx.request_id,
                    )
                    ctx.extra[NS_RAG_RETRIEVER]["code_hits"] = []

            # 注入为 system message 前缀(文本 + 代码)
            merged_chunks = list(retrieved_chunks)
            for hit in code_hits:
                snippet = self._format_code_hit(hit)
                if snippet:
                    merged_chunks.append(snippet)

            if merged_chunks:
                self._inject_system_message(ctx, merged_chunks)
                logger.debug(
                    "RAG 检索完成: query=%s, num_text=%d, num_code=%d, request_id=%s",
                    user_query[:50],
                    len(retrieved_chunks),
                    len(code_hits),
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

    # ------------------------------------------------------------------
    # Code RAG retrieval helpers
    # ------------------------------------------------------------------

    def _qdrant_url(self) -> str:
        return (
            os.environ.get("AI_GATEWAY_QDRANT_URL")
            or os.environ.get("QDRANT_URL")
            or "http://localhost:6333"
        )

    async def _list_code_collections(self) -> List[str]:
        """从 Qdrant 拉集合列表并挑 rag_code_* 前缀."""
        try:
            import httpx  # 已在 requirements.txt 里
        except Exception:
            return []

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._qdrant_url()}/collections/")
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("列出 Qdrant 集合失败,跳过代码检索: %s", exc)
            return []

        names = [
            c.get("name")
            for c in (data.get("result", {}) or {}).get("collections", []) or []
            if c.get("name")
        ]
        # 只选与本插件配置的 embedding_model 匹配的集合;不同维度模型会导致
        # Qdrant 搜索直接 4xx,tolerant 分支会把它们静默吞掉。见
        # _select_code_collections_for_model docstring。
        embedding_model = getattr(self._config, "embedding_model", "") or ""
        return _select_code_collections_for_model(names, embedding_model)

    def _encode_query(self, query: str) -> Optional[List[float]]:
        """按插件配置的 embedding_backend 编码一次查询向量.

        当前仅实现 local 分支(sentence-transformers),这是仓库主用路径。
        其他 backend 未实现时返回 None,导致代码检索被跳过(tolerant)。
        """
        backend = getattr(self._config, "embedding_backend", "local")
        model_name = getattr(self._config, "embedding_model", "Qwen/Qwen3-Embedding-0.6B")
        if backend != "local":
            logger.debug("code_rag: embedding_backend=%s 未实现查询编码,跳过", backend)
            return None
        try:
            from aigateway_core.pipelines.understanding.code_rag.embedding_router import encode_texts

            vectors = encode_texts(model_name, [query])
            return list(vectors[0]) if vectors else None
        except Exception as exc:
            logger.warning("code_rag: 查询编码失败,跳过: %s", exc)
            return None

    async def _retrieve_code_hits(self, query: str) -> List[Dict[str, Any]]:
        """并行查询所有 rag_code_* 集合并归并成 payload dict 列表."""
        collections = await self._list_code_collections()
        if not collections:
            return []

        vector = await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._encode_query(query)
        )
        if not vector:
            return []

        top_k = int(getattr(self._config, "code_rag_top_k", 5) or 5)
        threshold = float(getattr(self._config, "similarity_threshold", 0.7) or 0.7)

        async def _search_one(coll: str) -> List[Dict[str, Any]]:
            try:
                import httpx

                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        f"{self._qdrant_url()}/collections/{coll}/points/search",
                        json={
                            "vector": vector,
                            "limit": top_k,
                            "with_payload": True,
                            "score_threshold": threshold,
                        },
                    )
                    if resp.status_code == 404:
                        return []
                    resp.raise_for_status()
                    payload = resp.json()
            except Exception as exc:
                logger.warning("code_rag: 查询集合 %s 失败,跳过: %s", coll, exc)
                return []

            out: List[Dict[str, Any]] = []
            for item in payload.get("result", []) or []:
                p = item.get("payload") or {}
                if p:
                    p["_collection"] = coll
                    p["_score"] = item.get("score")
                    out.append(p)
            return out

        results = await asyncio.gather(*(_search_one(c) for c in collections))
        flat: List[Dict[str, Any]] = []
        for lst in results:
            flat.extend(lst)
        # 每个集合 top_k → 全局按 score 截 code_rag_top_k
        flat.sort(key=lambda p: (p.get("_score") or 0), reverse=True)
        return flat[:top_k]

    async def _fetch_related_code_chunks(
        self,
        collection_name: str,
        related_symbols: List[Dict[str, Any]],
        base_hit: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        if not related_symbols:
            return []

        try:
            import httpx
        except Exception:
            return []

        file_to_symbols: Dict[str, set[str]] = defaultdict(set)
        for item in related_symbols:
            file_path = str(item.get("file_path", "") or "")
            symbol_name = str(item.get("symbol_name", "") or "")
            if file_path and symbol_name:
                file_to_symbols[file_path].add(symbol_name)
        if not file_to_symbols:
            return []

        out: List[Dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=10.0) as client:
            for file_path, symbols in file_to_symbols.items():
                try:
                    resp = await client.post(
                        f"{self._qdrant_url()}/collections/{collection_name}/points/scroll",
                        json={
                            "limit": 200,
                            "with_payload": True,
                            "filter": {
                                "must": [
                                    {
                                        "key": "document_id",
                                        "match": {"value": base_hit.get("document_id")},
                                    },
                                    {"key": "file_path", "match": {"value": file_path}},
                                ]
                            },
                        },
                    )
                    if resp.status_code == 404:
                        continue
                    resp.raise_for_status()
                    payload = resp.json()
                except Exception as exc:
                    logger.warning(
                        "code_rag: scroll related chunks 失败 coll=%s file=%s: %s",
                        collection_name,
                        file_path,
                        exc,
                    )
                    continue

                for item in payload.get("result", {}).get("points", []) or []:
                    chunk = dict(item.get("payload") or {})
                    symbol = chunk.get("function_name") or chunk.get("class_name")
                    if symbol not in symbols:
                        continue
                    chunk["_collection"] = collection_name
                    chunk["_score"] = base_hit.get("_score")
                    chunk["_graph_related"] = True
                    out.append(chunk)
        return out

    async def _expand_code_hits_with_graph(
        self, hits: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """导入 metadata，并按 code_rag_graph_hops 抓取 related symbol chunks."""
        if not hits:
            return hits

        graph_dir = getattr(self._config, "code_graph_db_dir", "/data/code_graphs")
        hops = int(getattr(self._config, "code_rag_graph_hops", 1) or 1)
        try:
            from aigateway_core.pipelines.understanding.code_rag.graph_query import (
                lookup_related_symbols,
                lookup_symbol_metadata,
            )
        except Exception as exc:
            logger.warning("code_rag: 无法加载 graph_query,跳过图谱展开: %s", exc)
            return hits

        expanded: List[Dict[str, Any]] = []
        seen = {_code_hit_identity(hit) for hit in hits}
        for hit in hits:
            doc_id = str(hit.get("document_id", ""))
            graph_db_path = os.path.join(graph_dir, f"{doc_id}.db")
            symbol = hit.get("function_name") or hit.get("class_name")
            try:
                meta = lookup_symbol_metadata(
                    graph_db_path,
                    str(hit.get("file_path", "")),
                    symbol if symbol else None,
                    str(hit.get("chunk_text", "")),
                )
                enriched = _expand_code_hit_metadata(hit, meta)
                expanded.append(enriched)

                if not symbol or hops <= 0:
                    continue
                related_symbols = lookup_related_symbols(
                    graph_db_path,
                    str(hit.get("file_path", "")),
                    str(symbol),
                    hops=hops,
                )
                related_chunks = await self._fetch_related_code_chunks(
                    str(hit.get("_collection", "")),
                    related_symbols,
                    enriched,
                )
                for chunk in related_chunks:
                    ident = _code_hit_identity(chunk)
                    if ident in seen:
                        continue
                    seen.add(ident)
                    expanded.append(chunk)
            except Exception as exc:
                logger.warning(
                    "code_rag: 图谱查询失败 doc=%s symbol=%s: %s", doc_id, symbol, exc
                )
                expanded.append(hit)
        return expanded

    def _format_code_hit(self, hit: Dict[str, Any]) -> str:
        """把代码 hit 组装成一段可读的检索上下文."""
        file_path = hit.get("file_path") or hit.get("filename") or "<unknown>"
        start = hit.get("start_line")
        end = hit.get("end_line")
        header = f"[代码片段] {file_path}"
        if start is not None and end is not None:
            header += f" (L{start}-L{end})"
        symbol = hit.get("function_name") or hit.get("class_name")
        if symbol:
            header += f" :: {symbol}"
        neighbors: List[str] = []
        if hit.get("callers"):
            neighbors.append("callers=" + ", ".join(hit["callers"][:5]))
        if hit.get("callees"):
            neighbors.append("callees=" + ", ".join(hit["callees"][:5]))
        body = str(hit.get("chunk_text", "") or "").strip()
        if not body:
            return ""
        lines = [header]
        if neighbors:
            lines.append("(" + " ; ".join(neighbors) + ")")
        lines.append(body)
        return "\n".join(lines)

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
