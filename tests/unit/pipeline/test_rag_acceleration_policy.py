"""Failure-policy tests for accelerated RAG operations."""

from __future__ import annotations

import builtins
from typing import Any

import pytest
from aigateway_core.pipelines.understanding.rag.rag_retriever_plugin import (
    RAGAccelerationError,
    RAGRetrieverPlugin,
    _is_gpu_oom,
)
from aigateway_core.shared.integration_configs import RAGRetrieverConfig


def _block_llama_index(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def guarded_import(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name.startswith("llama_index"):
            raise ImportError("test dependency unavailable")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)


def test_explicit_cuda_reranker_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _block_llama_index(monkeypatch)
    plugin = RAGRetrieverPlugin(RAGRetrieverConfig(rerank_device="cuda"))

    with pytest.raises(
        RAGAccelerationError,
        match="rag_reranker_dependency_unavailable",
    ):
        plugin._rerank_sync("query", [object()])


def test_auto_reranker_keeps_optional_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_llama_index(monkeypatch)
    nodes = [object()]
    plugin = RAGRetrieverPlugin(RAGRetrieverConfig(rerank_device="auto"))

    assert plugin._rerank_sync("query", nodes) is nodes


def test_remote_reranker_preserves_node_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"results": [{"index": 1}, {"index": 0}]}

    monkeypatch.setattr("httpx.post", lambda *args, **kwargs: Response())
    first, second = object(), object()
    plugin = RAGRetrieverPlugin(
        RAGRetrieverConfig(
            rerank_backend="remote",
            rerank_device="remote",
            rerank_api_base="http://native:8189/v1",
            rerank_api_key="test-only",
        )
    )

    assert plugin._rerank_sync("query", [first, second]) == [second, first]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("CUDA out of memory", True),
        ("MPS backend out of memory", True),
        ("ordinary model error", False),
    ],
)
def test_gpu_oom_detection(message: str, expected: bool) -> None:
    assert _is_gpu_oom(RuntimeError(message)) is expected
