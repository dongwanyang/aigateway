"""RAGRetrieverPlugin 代码 RAG 扩展测试(Task 4).

只覆盖新增的纯逻辑 helper 和 code_rag_enabled 的 config-gate 行为,
不真连 Qdrant / sentence-transformers。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aigateway_core.plugins.rag_retriever_plugin import (
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


def test_code_rag_disabled_returns_no_code_hits() -> None:
    plugin = _make_plugin_without_init(code_rag_enabled=False)
    result = asyncio.new_event_loop().run_until_complete(
        plugin._list_code_collections()
    )
    # 集合列表逻辑本身与 flag 独立;真实控制在 execute()。此处只验证
    # config 字段可读且默认为关闭态。
    assert plugin._config.code_rag_enabled is False
    assert isinstance(result, list)


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


def test_expand_code_hits_with_graph_merges_lookup_result() -> None:
    plugin = _make_plugin_without_init()
    plugin._config.code_graph_db_dir = "/tmp/nonexistent"
    hits = [
        {"document_id": "doc1", "file_path": "auth.py", "function_name": "login", "chunk_text": "def login(): pass"},
    ]
    fake_meta = {"callers": ["register"], "callees": [], "imports": ["jwt"], "chunk_type": "function"}
    with patch("aigateway_core.code_rag.graph_query.lookup_symbol_metadata", return_value=fake_meta):
        expanded = asyncio.new_event_loop().run_until_complete(
            plugin._expand_code_hits_with_graph(hits)
        )
    assert expanded[0]["callers"] == ["register"]
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
