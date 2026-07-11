"""Code RAG API 路由测试(Task 3).

用 FastAPI 的 dependency_overrides 绕开 authenticate_admin,
用 fake 的 redis_manager / qdrant_manager / config_manager 灌 app.state,
不启动真实 Redis / Qdrant / codegraph / sentence-transformers。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aigateway_api.auth_middleware import authenticate_admin
from aigateway_api.code_rag_routes import router as code_rag_router


CODE_RAG_CFG = {
    "enabled": True,
    "allowed_server_paths": ["/tmp"],
    "max_file_size_mb": 5,
    "max_total_size_mb": 200,
    "max_file_count": 5000,
    "ignore_patterns": ["node_modules", ".git", "__pycache__", "dist", "build"],
    "graph_db_dir": "/tmp/code_graphs",
}


class _FakeRedis:
    """够用的 async Redis stub。"""

    def __init__(self) -> None:
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.lists: Dict[str, List[str]] = {}

    async def hset(self, key: str, mapping: Dict[str, Any]) -> None:
        h = self.hashes.setdefault(key, {})
        h.update({k: str(v) for k, v in mapping.items()})

    async def hgetall(self, key: str) -> Dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def expire(self, key: str, ttl: int) -> None:  # noqa: ARG002
        return None

    async def lpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).insert(0, value)

    async def lrange(self, key: str, start: int, end: int) -> List[str]:
        items = self.lists.get(key, [])
        if end == -1:
            return list(items[start:])
        return list(items[start : end + 1])

    async def lrem(self, key: str, count: int, value: str) -> None:  # noqa: ARG002
        items = self.lists.get(key, [])
        if value in items:
            items.remove(value)


class _FakeRedisManager:
    def __init__(self) -> None:
        self.redis = _FakeRedis()


class _FakeQdrantManager:
    """够用的 Qdrant stub;测试里我们不真调 upsert/list。"""

    def __init__(self) -> None:
        self._http = MagicMock()
        self.upsert_collection = AsyncMock(return_value=True)
        self.delete_by_filter = AsyncMock(return_value=0)


class _FakeConfigManager:
    def __init__(self, code_rag_cfg: Dict[str, Any]) -> None:
        self._cfg = {"code_rag": code_rag_cfg}

    def get(self, key: str, default: Any = None) -> Any:
        return self._cfg.get(key, default)


def _make_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FastAPI]:
    """构造一个精简的 FastAPI 应用,只挂 code_rag_router,让 auth 通过。"""
    app = FastAPI()
    app.include_router(code_rag_router, prefix="/admin")
    app.state.redis_manager = _FakeRedisManager()
    app.state.qdrant_manager = _FakeQdrantManager()
    app.state.config_manager = _FakeConfigManager(CODE_RAG_CFG)

    async def _allow_any() -> Dict[str, Any]:
        return {"user_id": "admin"}

    app.dependency_overrides[authenticate_admin] = _allow_any

    # 屏蔽后台任务真的跑起来触发依赖,避免测试环境跑 codegraph/git 之类的 lazy import
    monkeypatch.setattr(
        "aigateway_api.code_rag_routes._run_code_import_task",
        AsyncMock(return_value=None),
    )

    return TestClient(app), app


def test_post_code_import_server_path_returns_task_id(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _make_client(monkeypatch)
    response = client.post(
        "/admin/rag/code/import",
        json={
            "source_type": "server_path",
            "server_path": "/tmp",
            "embedding_model": "Qwen/Qwen3-Embedding-0.6B",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["task_id"]


def test_post_code_import_rejects_disallowed_server_path(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _make_client(monkeypatch)
    response = client.post(
        "/admin/rag/code/import",
        json={
            "source_type": "server_path",
            "server_path": "/etc",
            "embedding_model": "Qwen/Qwen3-Embedding-0.6B",
        },
    )
    assert response.status_code == 403
    assert "server_path" in response.json()["detail"]


def test_post_code_import_rejects_non_https_git(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _make_client(monkeypatch)
    response = client.post(
        "/admin/rag/code/import",
        json={
            "source_type": "git",
            "git_url": "git@github.com:org/repo.git",
            "embedding_model": "Qwen/Qwen3-Embedding-0.6B",
        },
    )
    assert response.status_code == 400
    assert "https" in response.json()["detail"].lower()


def test_post_code_import_rejects_unsupported_json_source_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _make_client(monkeypatch)
    response = client.post(
        "/admin/rag/code/import",
        json={
            "source_type": "folder",
            "embedding_model": "Qwen/Qwen3-Embedding-0.6B",
        },
    )
    assert response.status_code == 400
    assert "server_path/git" in response.json()["detail"]


# ---------------------------------------------------------------------------
# folder multipart 上传回归 (FastAPI 0.139 / Starlette 1.3+ 双 UploadFile 类):
#   request.form() 返回 starlette.datastructures.UploadFile, 而 code_rag_routes
#   顶部 import 的是 fastapi.UploadFile —— 两者在 0.139 是不同的类. 早期代码用
#   isinstance(u, UploadFile) 过滤, 全部 False → files=[] → 恒报
#   "folder 源必须至少上传一个文件", 前端 folder 上传 100% 失败.
#   修复: duck-typing (has read + filename). 本测试用真实 multipart body 锁住.
# ---------------------------------------------------------------------------


def test_post_code_import_folder_multipart_accepts_files(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("multipart")  # request.form() 需要 python-multipart
    client, _ = _make_client(monkeypatch)
    # 真实 multipart/form-data: source_type=folder + files + relative_paths
    response = client.post(
        "/admin/rag/code/import",
        data={
            "source_type": "folder",
            "embedding_model": "Qwen/Qwen3-Embedding-0.6B",
            "relative_paths": "main.py",
        },
        files={"files": ("main.py", b"def login(u):\n    return u\n", "text/x-python")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["task_id"]


def test_post_code_import_folder_multipart_rejects_zero_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("multipart")
    client, _ = _make_client(monkeypatch)
    # source_type=folder 但没带任何 file → 必须返回 4xx (具体 detail 取决于客户端
    # 是否发了 multipart body: TestClient files=[] 会退回 JSON 分支报 "请求体无效",
    # 真实浏览器发空 multipart 会报 "至少上传一个文件". 两者都是 4xx 拒绝.)
    response = client.post(
        "/admin/rag/code/import",
        data={"source_type": "folder", "embedding_model": "Qwen/Qwen3-Embedding-0.6B"},
        files=[],
    )
    assert response.status_code in (400, 422), response.text


def test_post_code_import_zip_multipart_accepts_file(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("multipart")
    import io
    import zipfile

    client, _ = _make_client(monkeypatch)
    # 造一个真实可解压的 zip (含一个 main.py)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("main.py", "def login(u):\n    return u\n")
    response = client.post(
        "/admin/rag/code/import",
        data={"source_type": "zip", "embedding_model": "Qwen/Qwen3-Embedding-0.6B"},
        files={"file": ("repo.zip", buf.getvalue(), "application/zip")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pending"


def test_get_code_task_returns_404_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _make_client(monkeypatch)
    response = client.get("/admin/rag/code/tasks/does-not-exist")
    # 缺失任务返回 404 (而非旧的假 pending), 让前端 pollTask 能识别真死任务并 dismiss,
    # 不再无限轮询转圈。见 get_code_task 的 HTTPException(404)。
    assert response.status_code == 404
    detail = response.json().get("detail", "")
    assert "does-not-exist" in detail or "not found" in detail.lower()


def _run(coro: Any) -> Any:
    """py3.14+ 兼容的同步调用异步 helper。"""
    return asyncio.new_event_loop().run_until_complete(coro)


def test_get_code_task_reflects_written_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client, app = _make_client(monkeypatch)
    # 手写一个 completed 任务状态
    _run(
        app.state.redis_manager.redis.hset(
            "aigateway:rag:code:tasks:t1",
            mapping={"status": "completed", "done": "3", "total": "3", "current_file": ""},
        )
    )
    response = client.get("/admin/rag/code/tasks/t1")
    body = response.json()
    assert body["status"] == "completed"
    assert body["done"] == 3
    assert body["total"] == 3
    assert body["current_file"] is None


def test_list_code_repositories_returns_array(monkeypatch: pytest.MonkeyPatch) -> None:
    client, app = _make_client(monkeypatch)
    fake_redis = app.state.redis_manager.redis
    _run(
        fake_redis.lpush(
            "aigateway:rag:code:documents",
            json.dumps({"document_id": "code_abc", "source_type": "server_path"}),
        )
    )
    response = client.get("/admin/rag/code/repositories")
    assert response.status_code == 200
    items = response.json()
    assert isinstance(items, list)
    assert items and items[0]["document_id"] == "code_abc"


def test_delete_code_repository_removes_redis_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, app = _make_client(monkeypatch)
    fake_redis = app.state.redis_manager.redis
    _run(
        fake_redis.lpush(
            "aigateway:rag:code:documents",
            json.dumps({"document_id": "code_del_me", "source_type": "server_path"}),
        )
    )
    # 让 Qdrant 集合枚举返回一个 rag_code_* 集合
    qdrant_mgr = app.state.qdrant_manager
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = MagicMock(
        return_value={"result": {"collections": [{"name": "rag_code_test"}, {"name": "rag_documents"}]}}
    )
    qdrant_mgr._http.get = AsyncMock(return_value=fake_resp)

    response = client.delete("/admin/rag/code/repositories/code_del_me")
    assert response.status_code == 204
    qdrant_mgr.delete_by_filter.assert_awaited()
    # Redis 元数据已删
    remaining = _run(fake_redis.lrange("aigateway:rag:code:documents", 0, -1))
    assert all("code_del_me" not in item for item in remaining)


# ---------------------------------------------------------------------------
# Task GC protection (Review finding 4):
#   asyncio.create_task 返回值必须被 strong-ref,否则 GC 可能在导入未跑完时
#   把 Task 收掉("Task was destroyed but it is pending")。_spawn_import_task
#   把每个 Task 放进 app.state.code_rag_active_tasks,done 后自动移除。
# ---------------------------------------------------------------------------


def test_spawn_import_task_tracks_task_in_app_state() -> None:
    import asyncio as _asyncio
    from types import SimpleNamespace

    from aigateway_api.code_rag_routes import _spawn_import_task

    app_state = SimpleNamespace()

    finished = _asyncio.Event()

    async def _payload() -> None:
        await _asyncio.sleep(0)
        finished.set()

    async def _drive() -> tuple[dict, "_asyncio.Task[None]"]:
        task = _spawn_import_task(app_state, _payload(), task_id="t-abc")
        # spawn 后立刻应该在 active 映射里 (key=task_id, value=Task)
        assert app_state.code_rag_active_tasks.get("t-abc") is task
        await task
        return app_state.code_rag_active_tasks, task

    active, task = _asyncio.new_event_loop().run_until_complete(_drive())
    assert task.done()
    assert finished.is_set()
    # done_callback 应该已经把 task 从映射里摘掉
    assert "t-abc" not in active


def test_spawn_import_task_logs_uncaught_exception(caplog) -> None:
    import asyncio as _asyncio
    import logging
    from types import SimpleNamespace

    from aigateway_api.code_rag_routes import _spawn_import_task

    app_state = SimpleNamespace()

    async def _boom() -> None:
        raise RuntimeError("kaboom")

    async def _drive() -> None:
        task = _spawn_import_task(app_state, _boom(), task_id="t-fail")
        try:
            await task
        except RuntimeError:
            pass

    with caplog.at_level(logging.ERROR, logger="aigateway_api.code_rag_routes"):
        _asyncio.new_event_loop().run_until_complete(_drive())
    assert any(
        "code rag import task t-fail raised uncaught exception" in r.getMessage()
        for r in caplog.records
    ), f"缺少未捕获异常的兜底日志: {[r.getMessage() for r in caplog.records]}"


def test_run_code_import_task_passes_real_symbol_name_to_graph_lookup(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """回归 Review 发现 1:_run_code_import_task 里 symbol_name 只取
    chunk["function_name"] / chunk["class_name"]。这些字段必须被
    splitter 真的填上,否则 lookup_symbol_metadata_strict 拿到 None 短路,
    整条 graph 增强链路是死的。

    这里 stub 掉重资产依赖,验证 strict lookup 被至少调用一次并且
    收到了非空的 symbol_name。
    """
    import asyncio as _asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from aigateway_api import code_rag_routes as routes_mod

    # ---- 桩:splitter 产出的 chunk 携带 function_name ----
    def _fake_split(source_dir: str, ignore_patterns: list[str]) -> list[dict]:
        return [
            {
                "file_path": "auth.py",
                "filename": "auth.py",
                "language": "python",
                "chunk_index": 0,
                "chunk_text": "def login(user):\n    return user\n",
                "start_line": 1,
                "end_line": 2,
                "function_name": "login",
                "class_name": None,
            }
        ]

    monkeypatch.setattr(
        "aigateway_core.pipelines.understanding.code_rag.splitter.split_code_directory", _fake_split
    )
    monkeypatch.setattr(
        "aigateway_core.pipelines.understanding.code_rag.graph_builder.build_code_graph",
        lambda src, dst: dst,
    )
    monkeypatch.setattr(
        "aigateway_core.pipelines.understanding.code_rag.embedding_router.probe_embedding_dimension",
        lambda model: 4,
    )
    monkeypatch.setattr(
        "aigateway_core.pipelines.understanding.code_rag.embedding_router.encode_texts",
        lambda model, texts: [[0.1, 0.2, 0.3, 0.4] for _ in texts],
    )

    strict_lookup_calls: list[tuple] = []

    def _fake_strict(graph_db_path, file_path, symbol_name, chunk_text):
        strict_lookup_calls.append((file_path, symbol_name))
        return {
            "callers": ["register"],
            "callees": [],
            "imports": [],
            "chunk_type": "function",
            "function_name": symbol_name,
            "class_name": None,
        }

    monkeypatch.setattr(
        "aigateway_core.pipelines.understanding.code_rag.graph_query.lookup_symbol_metadata_strict",
        _fake_strict,
    )

    # ---- 桩:app.state ----
    app_state = SimpleNamespace()
    app_state.redis_manager = _FakeRedisManager()
    qdrant_mgr = _FakeQdrantManager()
    put_resp = MagicMock()
    put_resp.raise_for_status = MagicMock()
    qdrant_mgr._http.put = AsyncMock(return_value=put_resp)
    app_state.qdrant_manager = qdrant_mgr

    async def _drive() -> None:
        await routes_mod._run_code_import_task(
            app_state=app_state,
            task_id="t-sym",
            document_id="code_sym",
            source_dir=str(tmp_path),
            source_type="server_path",
            source_label="server_path:///tmp/x",
            embedding_model="Qwen/Qwen3-Embedding-0.6B",
            ignore_patterns=[],
            graph_db_dir=str(tmp_path),
            cleanup_dirs=[],
        )

    _asyncio.new_event_loop().run_until_complete(_drive())

    assert strict_lookup_calls, "strict lookup 完全没被调用,链路断了"
    # 关键断言:传给 strict lookup 的 symbol_name 不能是 None
    file_paths, symbol_names = zip(*strict_lookup_calls)
    assert "login" in symbol_names, (
        f"symbol_name 期望包含真实符号 'login',实际收到: {symbol_names}. "
        "如果全是 None,说明 splitter 没写 function_name/class_name,"
        "graph 增强永远不会触发。"
    )

    # upsert 的 payload 里 callers 也必须落盘,证明 graph_meta 真被合并了
    upsert_call = qdrant_mgr._http.put.await_args
    body = upsert_call.kwargs["json"] if "json" in upsert_call.kwargs else upsert_call.args[1]
    if not isinstance(body, dict):
        body = {"points": []}
    written_points = body.get("points", [])
    assert written_points, "没有点被写入 Qdrant"
    payload = written_points[0]["payload"]
    assert payload["function_name"] == "login"
    assert payload["callers"] == ["register"]
