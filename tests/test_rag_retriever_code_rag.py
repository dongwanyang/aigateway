"""RAGRetrieverPlugin 代码 RAG 扩展测试(Task 4).

只覆盖新增的纯逻辑 helper 和 code_rag_enabled 的 config-gate 行为,
不真连 Qdrant / sentence-transformers。
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aigateway_core.pipelines.understanding.rag.rag_retriever_plugin import (
    RAGRetrieverPlugin,
    _dedupe_hits_by_identity,
    _expand_code_hit_metadata,
    _filter_code_collections,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_filter_code_collections_only_keeps_rag_code_prefix() -> None:
    names = ["rag_documents", "rag_code_qwen", "rag_code_openai", "semantic_cache"]
    assert _filter_code_collections(names) == ["rag_code_qwen", "rag_code_openai"]


def test_filter_code_collections_ignores_non_strings() -> None:
    names: List[Any] = [None, 42, "rag_code_x", ""]
    assert _filter_code_collections(names) == ["rag_code_x"]


def test_expand_code_hit_metadata_copies_graph_fields() -> None:
    hit = {"document_id": "d", "file_path": "auth.py", "function_name": "login"}
    expanded = _expand_code_hit_metadata(
        hit, {"callers": ["register"], "callees": ["hash_password"], "imports": ["jwt"]}
    )
    assert expanded["callers"] == ["register"]
    assert expanded["callees"] == ["hash_password"]
    assert expanded["imports"] == ["jwt"]
    # 原字段仍在
    assert expanded["document_id"] == "d"
    assert expanded["function_name"] == "login"


def test_expand_code_hit_metadata_defaults_missing_relationships() -> None:
    expanded = _expand_code_hit_metadata({"document_id": "d"}, {})
    assert expanded["callers"] == []
    assert expanded["callees"] == []
    assert expanded["imports"] == []


def test_dedupe_hits_by_identity_keeps_first_occurrence() -> None:
    a = {"document_id": "d1", "file_path": "a.py", "chunk_index": 0, "chunk_text": "first"}
    dup = {"document_id": "d1", "file_path": "a.py", "chunk_index": 0, "chunk_text": "dup"}
    b = {"document_id": "d1", "file_path": "b.py", "chunk_index": 0, "chunk_text": "second"}
    out = _dedupe_hits_by_identity([a, dup, b])
    assert len(out) == 2
    assert out[0]["chunk_text"] == "first"
    assert out[1]["chunk_text"] == "second"


# ---------------------------------------------------------------------------
# Plugin behavior
# ---------------------------------------------------------------------------


def _make_plugin_without_init(**cfg_overrides: Any) -> RAGRetrieverPlugin:
    """构造一个不去连接 Qdrant/llama_index 的 plugin 实例。"""
    with patch.object(RAGRetrieverPlugin, "_initialize_index", lambda self: None):
        plugin = RAGRetrieverPlugin(config=None)
    for k, v in cfg_overrides.items():
        setattr(plugin._config, k, v)
    plugin._is_available = True
    return plugin


def test_code_rag_disabled_skips_code_hit_retrieval() -> None:
    plugin = _make_plugin_without_init(code_rag_enabled=False)
    plugin._retrieve_code_hits = AsyncMock(return_value=[{"file_path": "auth.py"}])
    plugin._inject_system_message = MagicMock()

    class _Node:
        def __init__(self, text: str):
            self._text = text
            self.score = 1.0

        def get_content(self):
            return self._text

    class _Retriever:
        async def aretrieve(self, query: str):
            return [_Node(f"doc for {query}")]

    class _Index:
        def as_retriever(self, similarity_top_k: int):
            return _Retriever()

    plugin._index = _Index()

    context = SimpleNamespace(
        request={"messages": [{"role": "user", "content": "how login works?"}]},
        extra={},
        request_id="req-test",
    )
    result = asyncio.new_event_loop().run_until_complete(plugin.execute(context))

    plugin._retrieve_code_hits.assert_not_awaited()
    assert result is context
    assert context.extra["rag_retriever"]["retrieved_chunks"] == ["doc for how login works?"]
    assert "code_hits" not in context.extra["rag_retriever"]


def test_encode_query_returns_none_for_non_local_backend() -> None:
    plugin = _make_plugin_without_init(embedding_backend="openai")
    assert plugin._encode_query("hello") is None


def test_retrieve_code_hits_empty_when_no_collections() -> None:
    plugin = _make_plugin_without_init(code_rag_enabled=True)
    plugin._list_code_collections = AsyncMock(return_value=[])
    out = asyncio.new_event_loop().run_until_complete(plugin._retrieve_code_hits("q"))
    assert out == []


def test_expand_code_hits_with_graph_returns_input_when_empty() -> None:
    plugin = _make_plugin_without_init()
    result = asyncio.new_event_loop().run_until_complete(plugin._expand_code_hits_with_graph([]))
    assert result == []


def test_expand_code_hits_with_graph_invokes_lookup_when_hops_positive() -> None:
    """运行时验证 code_rag_graph_hops>0 时 graph 展开链路真的被触发,
    而不只是源码里有 'lookup_related_symbols' 字符串(被动断言)。

    锁死:hops>0 → 调用 lookup_related_symbols + _fetch_related_code_chunks,
    且相关 chunk 被并入结果;hops=0 → 两者都不被调用(只回原 hit)。
    """
    plugin = _make_plugin_without_init()
    plugin._config.code_graph_db_dir = "/tmp/code_graphs_test"
    plugin._config.code_rag_graph_hops = 1

    call_log: Dict[str, Any] = {"lookup": 0, "fetch": 0}

    def _fake_lookup(graph_repo_path, file_path, symbol, *, hops):
        call_log["lookup"] += 1
        call_log["lookup_args"] = {
            "graph_repo_path": graph_repo_path,
            "file_path": file_path,
            "symbol": symbol,
            "hops": hops,
        }
        return [
            {"symbol_name": "helper", "file_path": "utils.py"},
        ]

    async def _fake_fetch(collection_name, related_symbols, base_hit):
        call_log["fetch"] += 1
        # 模拟 scroll 命中:返回 helper 的 chunk(与原 hit 不同 doc_id/file_path 避免去重)
        return [
            {
                "document_id": base_hit.get("document_id"),
                "file_path": "utils.py",
                "function_name": "helper",
                "chunk_text": "def helper():\n    return 'ok'\n",
                "chunk_index": 0,
            }
        ]

    with patch(
        "aigateway_core.pipelines.understanding.code_rag.graph_query.lookup_related_symbols",
        _fake_lookup,
    ):
        plugin._fetch_related_code_chunks = _fake_fetch

        hits = [
            {
                "document_id": "code_sym",
                "file_path": "auth.py",
                "function_name": "login",
                "chunk_text": "def login():\n    pass\n",
                "chunk_index": 0,
                "callers": ["register"],
                "callees": ["helper"],
                "imports": [],
            }
        ]
        expanded = asyncio.new_event_loop().run_until_complete(
            plugin._expand_code_hits_with_graph(hits)
        )

    # hops>0 → lookup_related_symbols 被调用,且参数透传正确
    assert call_log["lookup"] == 1
    assert call_log["lookup_args"]["symbol"] == "login"
    assert call_log["lookup_args"]["hops"] == 1
    assert call_log["lookup_args"]["graph_repo_path"].endswith(
        os.path.join("code_graphs_test", "code_sym")
    ), "graph_repo_path 应由 code_graph_db_dir + document_id 拼出"
    # _fetch_related_code_chunks 被调用(scroll 相关 chunk)
    assert call_log["fetch"] == 1
    # helper 的 chunk 被并入结果(去重后保留原 hit + 新相关 chunk)
    symbols = {h.get("function_name") for h in expanded}
    assert {"login", "helper"}.issubset(symbols), f"相关符号未并入: {symbols}"
    # 注:_graph_related 标记由真实 _scroll_related_file 设置,本测试 mock 了
    # _fetch_related_code_chunks 所以不会带该标记 —— 这里验证的是"相关 chunk
    # 被并入结果"这一行为契约,而非 scroll 内部打标细节。


def test_expand_code_hits_with_graph_skips_lookup_when_hops_zero() -> None:
    """hops=0 时不能触发 graph lookup(纯 payload 直读,无多跳扩展)。"""
    plugin = _make_plugin_without_init()
    plugin._config.code_graph_db_dir = "/tmp/code_graphs_test"
    plugin._config.code_rag_graph_hops = 0

    call_log = {"lookup": 0}

    def _fake_lookup(*a, **kw):
        call_log["lookup"] += 1
        return []

    plugin._fetch_related_code_chunks = AsyncMock(return_value=[])

    with patch(
        "aigateway_core.pipelines.understanding.code_rag.graph_query.lookup_related_symbols",
        _fake_lookup,
    ):
        hits = [
            {
                "document_id": "code_sym",
                "file_path": "auth.py",
                "function_name": "login",
                "chunk_text": "def login():\n    pass\n",
                "chunk_index": 0,
                "callers": ["register"],
                "callees": [],
                "imports": [],
            }
        ]
        expanded = asyncio.new_event_loop().run_until_complete(
            plugin._expand_code_hits_with_graph(hits)
        )

    assert call_log["lookup"] == 0, "hops=0 不应触发 graph lookup"
    plugin._fetch_related_code_chunks.assert_not_awaited()
    # 原 hit 原样保留
    assert len(expanded) == 1
    assert expanded[0]["function_name"] == "login"


def test_expand_code_hits_with_graph_preserves_payload_relationships() -> None:
    """重构后:_expand_code_hits_with_graph 不再回查 graph lookup_symbol_metadata
    覆盖 payload,而是直接读 payload 里的 callers/callees/imports(导入时已存)。
    验证:payload 里的 callers/imports 原样保留(不被空查询覆盖成空)。
    """
    plugin = _make_plugin_without_init()
    plugin._config.code_graph_db_dir = "/tmp/nonexistent"
    # hops=0 跳过多跳扩展(无图谱时 lookup_related_symbols 返回空,不影响断言)
    plugin._config.code_rag_graph_hops = 0
    hits = [
        {
            "document_id": "doc1",
            "file_path": "auth.py",
            "function_name": "login",
            "chunk_text": "def login(): pass",
            "callers": ["register"],
            "callees": ["hash_password"],
            "imports": ["jwt"],
        },
    ]
    expanded = asyncio.new_event_loop().run_until_complete(
        plugin._expand_code_hits_with_graph(hits)
    )
    # payload 里的 callers/callees/imports 必须原样保留(不再被回查覆盖)
    assert expanded[0]["callers"] == ["register"]
    assert expanded[0]["callees"] == ["hash_password"]
    assert expanded[0]["imports"] == ["jwt"]


def test_format_code_hit_renders_header_body() -> None:
    plugin = _make_plugin_without_init()
    snippet = plugin._format_code_hit(
        {
            "file_path": "core/auth.py",
            "start_line": 3,
            "end_line": 8,
            "function_name": "login",
            "callers": ["register"],
            "callees": ["hash_password"],
            "chunk_text": "def login():\n    return True",
        }
    )
    assert "core/auth.py" in snippet
    assert "L3-L8" in snippet
    assert ":: login" in snippet
    assert "callers=register" in snippet
    assert "callees=hash_password" in snippet
    assert "def login" in snippet


def test_format_code_hit_returns_empty_when_no_body() -> None:
    plugin = _make_plugin_without_init()
    assert plugin._format_code_hit({"file_path": "x.py", "chunk_text": "   "}) == ""


# ---------------------------------------------------------------------------
# Cross-model retrieval safety (Review finding 3):
#   同一个查询向量不能盲扫所有 rag_code_* 集合——维度不匹配的集合会被
#   Qdrant 4xx,tolerant 分支会把它们静默吞掉。这里锁死:
#     1) 有匹配集合时,只保留匹配那份
#     2) 无匹配集合时,返回空列表(比全部盲扫更安全)
#     3) 空 embedding_model 时退化为原全集(不误伤,交给调用方处理)
# ---------------------------------------------------------------------------


def test_select_code_collections_picks_only_matching_model() -> None:
    from aigateway_core.pipelines.understanding.rag.rag_retriever_plugin import (
        _select_code_collections_for_model,
    )

    names = [
        "rag_documents",
        "rag_code_qwen_qwen3_embedding_0_6b",
        "rag_code_text_embedding_3_large",
        "rag_code_bge_small",
    ]
    picked = _select_code_collections_for_model(names, "Qwen/Qwen3-Embedding-0.6B")
    assert picked == ["rag_code_qwen_qwen3_embedding_0_6b"]


def test_select_code_collections_returns_empty_when_no_match() -> None:
    from aigateway_core.pipelines.understanding.rag.rag_retriever_plugin import (
        _select_code_collections_for_model,
    )

    names = ["rag_code_qwen_qwen3_embedding_0_6b", "rag_code_bge_small"]
    picked = _select_code_collections_for_model(names, "text-embedding-3-large")
    assert picked == []


def test_select_code_collections_falls_back_when_model_missing() -> None:
    from aigateway_core.pipelines.understanding.rag.rag_retriever_plugin import (
        _select_code_collections_for_model,
    )

    names = ["rag_code_qwen_qwen3_embedding_0_6b", "rag_documents"]
    picked = _select_code_collections_for_model(names, "")
    # 空 model → 保留所有 rag_code_* 前缀(过滤掉非代码集合),交给上层处理
    assert picked == ["rag_code_qwen_qwen3_embedding_0_6b"]


def test_list_code_collections_filters_by_configured_embedding_model() -> None:
    """回归:_list_code_collections 拉到的集合会按 self._config.embedding_model
    再过滤一层,避免向不同维度的集合投送同一个查询向量。
    """
    plugin = _make_plugin_without_init(embedding_model="Qwen/Qwen3-Embedding-0.6B")

    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = MagicMock(
        return_value={
            "result": {
                "collections": [
                    {"name": "rag_documents"},
                    {"name": "rag_code_qwen_qwen3_embedding_0_6b"},
                    {"name": "rag_code_text_embedding_3_large"},
                ]
            }
        }
    )

    class _FakeClient:
        def __init__(self, *_a, **_kw) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc) -> None:
            return None

        async def get(self, _url):
            return fake_resp

    with patch("httpx.AsyncClient", _FakeClient):
        picked = asyncio.new_event_loop().run_until_complete(
            plugin._list_code_collections()
        )
    assert picked == ["rag_code_qwen_qwen3_embedding_0_6b"]
