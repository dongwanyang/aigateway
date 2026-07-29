"""Behavioural coverage for Code RAG graph query and incremental sync APIs."""

from __future__ import annotations

import io
import sys
import zipfile
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from aigateway_api import code_rag_routes as routes
from fastapi import HTTPException


def _request(state):
    return SimpleNamespace(app=SimpleNamespace(state=state))


class _InlineLoop:
    async def run_in_executor(self, _executor, func):
        return func()


@pytest.mark.asyncio
async def test_graph_query_endpoints_delegate_with_exact_repository_and_options():
    request = _request(SimpleNamespace())
    query = MagicMock(return_value=[{"name": "target", "kind": "function"}])
    callers = MagicMock(return_value=[{"name": "caller"}])
    callees = MagicMock(return_value=[{"name": "callee"}])
    impact = MagicMock(return_value={"nodes": ["target", "caller"]})
    node = MagicMock(return_value={"name": "target", "file": "src/app.py"})
    files = MagicMock(return_value=[{"path": "src/app.py"}])

    with (
        patch.object(routes.asyncio, "get_running_loop", return_value=_InlineLoop()),
        patch.object(routes, "_resolve_graph_repo_path", return_value="/graph/repo"),
        patch(
            "aigateway_core.pipelines.understanding.code_rag.graph_query.query_symbols",
            new=query,
        ),
        patch(
            "aigateway_core.pipelines.understanding.code_rag.graph_query.get_callers",
            new=callers,
        ),
        patch(
            "aigateway_core.pipelines.understanding.code_rag.graph_query.get_callees",
            new=callees,
        ),
        patch(
            "aigateway_core.pipelines.understanding.code_rag.graph_query.get_impact",
            new=impact,
        ),
        patch(
            "aigateway_core.pipelines.understanding.code_rag.graph_query.get_node",
            new=node,
        ),
        patch(
            "aigateway_core.pipelines.understanding.code_rag.graph_query.list_files",
            new=files,
        ),
    ):
        found = await routes.query_code_symbols(
            "repo-1", request, symbol="target", kind="function", limit=5, _auth={}
        )
        found_callers = await routes.get_code_callers(
            "repo-1", request, symbol="target", _auth={}
        )
        found_callees = await routes.get_code_callees(
            "repo-1", request, symbol="target", _auth={}
        )
        found_impact = await routes.get_code_impact(
            "repo-1", request, symbol="target", depth=3, _auth={}
        )
        found_node = await routes.get_code_node(
            "repo-1", request, symbol="target", _auth={}
        )
        found_files = await routes.list_code_files("repo-1", request, _auth={})

    assert found == [{"name": "target", "kind": "function"}]
    assert found_callers == {"symbol": "target", "callers": [{"name": "caller"}]}
    assert found_callees == {"symbol": "target", "callees": [{"name": "callee"}]}
    assert found_impact == {"nodes": ["target", "caller"]}
    assert found_node == {"name": "target", "file": "src/app.py"}
    assert found_files == [{"path": "src/app.py"}]
    query.assert_called_once_with(
        "/graph/repo", "target", kind="function", limit=5
    )
    callers.assert_called_once_with("/graph/repo", "target")
    callees.assert_called_once_with("/graph/repo", "target")
    impact.assert_called_once_with("/graph/repo", "target", depth=3)
    node.assert_called_once_with("/graph/repo", "target")
    files.assert_called_once_with("/graph/repo")


@pytest.mark.asyncio
async def test_incremental_sync_refreshes_changed_symbols_and_removes_deleted_files(
    tmp_path,
):
    graph_path = tmp_path / "graphs" / "repo-1"
    graph_path.mkdir(parents=True)
    source_path = tmp_path / "source"
    source_path.mkdir()
    repo_meta = {
        "document_id": "repo-1",
        "source_type": "server_path",
        "graph_repo_path": str(graph_path),
        "workspace_path": str(source_path),
        "embedding_model": "embedding-model",
    }
    qdrant = MagicMock()
    qdrant._http = MagicMock()
    response = MagicMock()
    response.raise_for_status.return_value = None
    qdrant._http.put = AsyncMock(return_value=response)
    qdrant.delete_by_filter = AsyncMock()
    state = SimpleNamespace(
        redis_manager=MagicMock(),
        qdrant_manager=qdrant,
        config_manager=MagicMock(),
    )
    request = _request(state)
    read_hashes = MagicMock(side_effect=[
        {"src/changed.py": "old", "src/deleted.py": "old"},
        {"src/changed.py": "new"},
    ])
    run_sync = MagicMock()
    chunks = [{
        "embed_text": "function target()",
        "filename": "changed.py",
        "file_path": "changed.py",
        "language": "python",
        "chunk_index": 0,
        "chunk_text": "def target(): return 1",
        "function_name": "target",
        "class_name": None,
        "start_line": 1,
        "end_line": 1,
        "callers": ["caller"],
        "callees": [],
        "imports": [],
        "signature": "target()",
        "docstring": "",
    }]
    build_chunks = MagicMock(return_value=chunks)
    encode = MagicMock(return_value=[[0.1, 0.2, 0.3]])

    with (
        patch.object(routes.asyncio, "get_running_loop", return_value=_InlineLoop()),
        patch.object(routes, "_list_repositories", new=AsyncMock(return_value=[repo_meta])),
        patch.object(routes, "_load_code_rag_config", return_value={
            "graph_db_dir": str(tmp_path / "graphs"),
            "ignore_patterns": ["node_modules/**"],
        }),
        patch(
            "aigateway_core.pipelines.understanding.code_rag.embedding_router.resolve_collection_name",
            return_value="code_collection",
        ),
        patch(
            "aigateway_core.pipelines.understanding.code_rag.embedding_router.encode_texts",
            new=encode,
        ),
        patch(
            "aigateway_core.pipelines.understanding.code_rag.graph_query.read_file_hashes",
            new=read_hashes,
        ),
        patch(
            "aigateway_core.pipelines.understanding.code_rag.graph_query.run_codegraph_sync",
            new=run_sync,
        ),
        patch(
            "aigateway_core.pipelines.understanding.code_rag.splitter.build_symbol_chunks",
            new=build_chunks,
        ),
    ):
        result = await routes.sync_code_repository(
            "repo-1", request, _auth={}
        )

    assert result == {
        "document_id": "repo-1",
        "synced_files": 1,
        "refreshed_symbols": 1,
        "deleted_files": 1,
    }
    assert read_hashes.call_count == 2
    run_sync.assert_called_once_with(str(graph_path))
    build_chunks.assert_called_once_with(
        str(source_path),
        str(graph_path),
        ["node_modules/**"],
        only_files=["src/changed.py"],
    )
    encode.assert_called_once_with("embedding-model", ["function target()"])
    assert qdrant.delete_by_filter.await_count == 2
    qdrant.delete_by_filter.assert_any_await(
        "code_collection",
        {"must": [
            {"key": "document_id", "match": {"value": "repo-1"}},
            {"key": "file_path", "match": {"value": "changed.py"}},
        ]},
    )
    qdrant.delete_by_filter.assert_any_await(
        "code_collection",
        {"must": [
            {"key": "document_id", "match": {"value": "repo-1"}},
            {"key": "file_path", "match": {"value": "deleted.py"}},
        ]},
    )
    qdrant._http.put.assert_awaited_once()
    point = qdrant._http.put.await_args.kwargs["json"]["points"][0]
    assert point["vector"] == [0.1, 0.2, 0.3]
    assert point["payload"]["chunk_type"] == "function"
    assert point["payload"]["function_name"] == "target"


def test_git_materialization_uses_bounded_noninteractive_clone(tmp_path):
    destination = tmp_path / "managed-repo"
    clone = MagicMock()
    fake_git = SimpleNamespace(Repo=SimpleNamespace(clone_from=clone))
    with patch.dict(sys.modules, {"git": fake_git}):
        result = routes._materialize_git_repo(
            "https://example.test/repo.git",
            "release",
            timeout=5,
            dest_dir=str(destination),
        )

    assert result == str(destination)
    clone.assert_called_once_with(
        "https://example.test/repo.git",
        str(destination),
        depth=1,
        branch="release",
        env={
            "GIT_HTTP_LOW_SPEED_LIMIT": "1000",
            "GIT_HTTP_LOW_SPEED_TIME": "60",
            "GIT_TERMINAL_PROMPT": "0",
        },
    )


def test_zip_materialization_extracts_safe_members_and_rejects_zip_slip():
    safe_buffer = io.BytesIO()
    with zipfile.ZipFile(safe_buffer, "w") as archive:
        archive.writestr("repo/src/app.py", "print('ok')")
        archive.writestr("repo/empty/", "")

    extracted = routes._materialize_zip_upload(
        safe_buffer.getvalue(), max_total_mb=1
    )
    extracted_path = routes.Path(extracted)
    try:
        assert (extracted_path / "repo/src/app.py").read_text(
            encoding="utf-8"
        ) == "print('ok')"
    finally:
        routes.shutil.rmtree(extracted, ignore_errors=True)

    unsafe_buffer = io.BytesIO()
    with zipfile.ZipFile(unsafe_buffer, "w") as archive:
        archive.writestr("../escape.py", "bad")
    with pytest.raises(HTTPException) as caught:
        routes._materialize_zip_upload(
            unsafe_buffer.getvalue(), max_total_mb=1
        )
    assert caught.value.status_code == 400
    assert "zip-slip" in caught.value.detail


def test_workspace_symlink_is_idempotent_and_replaces_stale_directory(tmp_path):
    graph = tmp_path / "graph"
    source = tmp_path / "source"
    source.mkdir()
    stale = graph / "src"
    stale.mkdir(parents=True)
    (stale / "old.py").write_text("old", encoding="utf-8")

    routes._ensure_workspace_symlink(str(graph), str(source))
    link = graph / "src"
    assert link.is_symlink()
    assert link.resolve() == source.resolve()

    routes._ensure_workspace_symlink(str(graph), str(source))
    assert link.is_symlink()
    assert link.resolve() == source.resolve()


@pytest.mark.asyncio
async def test_folder_materialization_sanitizes_paths_and_preserves_tree():
    uploads = [
        SimpleNamespace(
            filename="app.py", read=AsyncMock(return_value=b"print('ok')")
        ),
        SimpleNamespace(
            filename="readme.md", read=AsyncMock(return_value=b"docs")
        ),
    ]
    relative_paths = [
        "/project/src/../app.py",
        r"project\docs\readme.md",
    ]
    assert routes._sanitize_relative_path(relative_paths[0]) == "project/src/app.py"
    assert routes._folder_source_label(uploads, relative_paths) == "folder://project"

    materialized = await routes._materialize_folder_upload(
        uploads,
        relative_paths,
        max_file_size_mb=1,
        max_total_size_mb=2,
        max_file_count=10,
    )
    root = routes.Path(materialized)
    try:
        assert (root / "project/src/app.py").read_bytes() == b"print('ok')"
        assert (root / "project/docs/readme.md").read_bytes() == b"docs"
    finally:
        routes.shutil.rmtree(materialized, ignore_errors=True)


@pytest.mark.asyncio
async def test_import_deadline_persists_failure_instead_of_silent_success():
    write_state = AsyncMock()
    with (
        patch.object(
            routes,
            "_run_code_import_task",
            new=AsyncMock(side_effect=routes.asyncio.TimeoutError),
        ),
        patch.object(routes, "_write_task_state", new=write_state),
    ):
        await routes._run_code_import_task_with_deadline(
            deadline_seconds=10,
            app_state=SimpleNamespace(),
            task_id="task-timeout",
            document_id="repo-1",
        )

    write_state.assert_awaited_once_with(
        ANY,
        "task-timeout",
        {
            "status": "failed",
            "error": "import task exceeded 10s deadline",
        },
    )


def test_task_response_normalizes_corrupt_persisted_counters():
    shaped = routes._shape_task_response(
        "task-1",
        {
            "status": "",
            "done": "not-a-number",
            "total": object(),
            "created_at": "bad",
            "current_file": "",
            "error": "",
            "source_label": "folder://repo",
            "source_type": "folder",
        },
    )
    assert shaped == {
        "task_id": "task-1",
        "status": "pending",
        "done": 0,
        "total": 0,
        "current_file": None,
        "error": None,
        "source_label": "folder://repo",
        "source_type": "folder",
        "created_at": 0,
    }
