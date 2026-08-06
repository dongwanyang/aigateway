"""Regression coverage for fail-closed Code RAG import completion."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aigateway_api import code_rag_routes
from aigateway_core.shared.auth.sqlite_store import SQLiteStore


@pytest.mark.asyncio
async def test_zero_chunk_import_cannot_be_marked_completed(tmp_path) -> None:
    store = SQLiteStore(db_path=str(tmp_path / "tasks.db"))
    app_state = SimpleNamespace(sqlite_store=store)

    await code_rag_routes._write_task_state(
        app_state,
        "task-empty",
        {
            "status": "splitting",
            "done": 0,
            "total": 0,
            "source_type": "git",
            "source_label": "https://example.test/empty.git",
        },
    )
    await code_rag_routes._write_task_state(
        app_state,
        "task-empty",
        {"status": "completed", "done": 0},
    )

    state = store.read_code_rag_task("task-empty")
    assert state is not None
    assert state["status"] == "failed"
    assert state["done"] == 0
    assert state["total"] == 0
    assert "未生成任何可索引代码符号" in state["error"]


@pytest.mark.asyncio
async def test_non_empty_import_can_still_be_marked_completed(tmp_path) -> None:
    store = SQLiteStore(db_path=str(tmp_path / "tasks.db"))
    app_state = SimpleNamespace(sqlite_store=store)

    await code_rag_routes._write_task_state(
        app_state,
        "task-indexed",
        {
            "status": "embedding",
            "done": 3,
            "total": 3,
            "source_type": "git",
            "source_label": "https://example.test/indexed.git",
        },
    )
    await code_rag_routes._write_task_state(
        app_state,
        "task-indexed",
        {"status": "completed", "done": 3},
    )

    state = store.read_code_rag_task("task-indexed")
    assert state is not None
    assert state["status"] == "completed"
    assert state["done"] == 3
    assert state["total"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "redis_manager",
    [None, SimpleNamespace(redis=None)],
)
async def test_repository_metadata_store_is_required(redis_manager) -> None:
    with pytest.raises(RuntimeError, match="unable to persist code repository metadata"):
        await code_rag_routes._append_repository(
            redis_manager,
            {
                "document_id": "code_missing_store",
                "source_type": "git",
                "source_label": "https://example.test/repo.git",
                "chunk_count": 1,
            },
        )
