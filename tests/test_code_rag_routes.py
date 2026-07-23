"""Code RAG API 路由测试(Task 3).

用 FastAPI 的 dependency_overrides 绕开 authenticate_admin,
用 fake 的 redis_manager / qdrant_manager / config_manager 灌 app.state,
不启动真实 Redis / Qdrant / codegraph / sentence-transformers。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import time
from pathlib import Path
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

    async def delete(self, *keys: str) -> None:  # noqa: ARG002
        for k in keys:
            self.hashes.pop(k, None)
            self.lists.pop(k, None)


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
    from aigateway_core.shared.auth.sqlite_store import SQLiteStore

    app = FastAPI()
    app.include_router(code_rag_router, prefix="/admin")
    app.state.redis_manager = _FakeRedisManager()
    app.state.qdrant_manager = _FakeQdrantManager()
    app.state.config_manager = _FakeConfigManager(CODE_RAG_CFG)
    # Task state 现在落 SQLite(与 main.py lifespan 一致)
    db_dir = tempfile.mkdtemp(prefix="code_rag_test_")
    app.state._test_db_dir = db_dir
    app.state.sqlite_store = SQLiteStore(db_path=str(Path(db_dir) / "tasks.db"))

    async def _allow_any() -> Dict[str, Any]:
        return {"user_id": "admin"}

    app.dependency_overrides[authenticate_admin] = _allow_any

    # 屏蔽后台任务真的跑起来触发依赖,避免测试环境跑 codegraph/git 之类的 lazy import
    monkeypatch.setattr(
        "aigateway_api.code_rag_routes._run_code_import_task",
        AsyncMock(return_value=None),
    )

    return TestClient(app), app


def _read_task(store, task_id: str) -> Dict[str, Any]:
    """从 SQLiteStore 读一行,缺失返回 {}。"""
    row = store.read_code_rag_task(task_id)
    return row or {}


def _make_app_state_with_store(tmp_path: Path) -> "SimpleNamespace":
    """构造一个带 sqlite_store 的 app.state(供 _run_code_import_task 集成测试)。"""
    from types import SimpleNamespace

    from aigateway_core.shared.auth.sqlite_store import SQLiteStore

    app_state = SimpleNamespace()
    app_state.redis_manager = _FakeRedisManager()
    app_state.sqlite_store = SQLiteStore(db_path=str(tmp_path / "tasks.db"))
    return app_state


def test_post_code_import_server_path_returns_task_id(monkeypatch: pytest.MonkeyPatch) -> None:
    client, app = _make_client(monkeypatch)
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

    # 不再只是断言 task_id truthy —— 验证真实的持久化与派生行为,
    # 否则 import 端点把任务状态写空 / 不 spawn 任务也能让本测试通过(fake-test 漏洞).
    task_id = body["task_id"]

    # 1) 任务状态确实落进了 SQLite code_rag_tasks,且关键字段由端点写入了正确值
    state = _read_task(app.state.sqlite_store, task_id)
    assert state, "import 端点没有把 task 状态写入 SQLite"
    assert state.get("status") == "pending"
    assert state.get("source_type") == "server_path"
    assert state.get("source_label") == "server_path:///tmp"
    assert state.get("embedding_model") == "Qwen/Qwen3-Embedding-0.6B"
    # created_at 必须是个正整数时间戳(证明 _write_task_state 真的写了 time.time())
    created_at = int(state.get("created_at") or 0)
    assert created_at > 0, f"created_at 未写入或非正: {state!r}"
    # document_id 由端点生成 (code_ 前缀 + hex),后续 cancel/query/list 都依赖它
    assert (state.get("document_id") or "").startswith("code_")

    # 后台任务被 _spawn_import_task strong-ref 的行为,已由
    # test_spawn_import_task_tracks_task_in_app_state 确定性覆盖;
    # 这里不再断言 active_tasks —— TestClient 同步返回时被 patch 成 no-op 的
    # 后台任务可能已完成并被 done_callback 摘除,断言它"还在"是在测事件循环时序。


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
    client, app = _make_client(monkeypatch)
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

    # 验证 folder 源把 source_type/source_label 真正落进了 SQLite,
    # 否则前端轮询拿到的任务元数据全是空(fake-test 漏洞).
    task_id = body["task_id"]
    state = _read_task(app.state.sqlite_store, task_id)
    assert state.get("source_type") == "folder"
    assert state.get("source_label") == "folder://main.py"
    assert state.get("embedding_model") == "Qwen/Qwen3-Embedding-0.6B"


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

    client, app = _make_client(monkeypatch)
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
    assert body["task_id"]

    # 验证 zip 源把 source_type/source_label 真正落进了 SQLite.
    task_id = body["task_id"]
    state = _read_task(app.state.sqlite_store, task_id)
    assert state.get("source_type") == "zip"
    assert state.get("source_label") == "zip://repo.zip"
    assert state.get("embedding_model") == "Qwen/Qwen3-Embedding-0.6B"


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
    # 手写一个 completed 任务状态到 SQLite
    now = int(time.time())
    app.state.sqlite_store.upsert_code_rag_task({
        "task_id": "t1", "document_id": "d1", "status": "completed",
        "done": 3, "total": 3, "current_file": "", "source_type": "git",
        "source_label": "", "embedding_model": "", "graph_repo_path": "",
        "error": "", "created_at": now, "updated_at": now,
    })
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


def test_delete_code_repository_rejects_unregistered_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review round 3 C1:document_id 未在 Redis 注册时必须 404,不能落到 shutil.rmtree。
    `..` 是合法单段 path param,旧实现会 rmtree 掉上级数据卷。"""
    client, app = _make_client(monkeypatch)
    # 不注册任何仓库 → delete 应 404,绝不触碰文件系统
    with tempfile.TemporaryDirectory() as guard_dir:
        # 把 graph_db_dir 指向临时目录,确保即使守卫被绕过也不污染真实 /data/code_graphs
        app.state.config_manager._cfg["code_rag"]["graph_db_dir"] = guard_dir
        # 在 guard_dir 的上级放一个诱饵目录,验证它绝不会被删
        bait = Path(guard_dir).parent / f"bait_{Path(guard_dir).name}"
        bait.mkdir(parents=True, exist_ok=True)
        try:
            # 未注册的 document_id
            resp = client.delete("/admin/rag/code/repositories/code_never_registered")
            assert resp.status_code == 404
            # 路径穿越尝试
            resp2 = client.delete("/admin/rag/code/repositories/..")
            assert resp2.status_code == 404
            # 诱饵目录仍在(未被 rmtree)
            assert bait.exists()
        finally:
            shutil.rmtree(bait, ignore_errors=True)


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
    """重构后:_run_code_import_task 走 build_symbol_chunks(返回带 callers/callees
    与 embed_text 的 chunk),embedding 嵌 embed_text(结构描述)而非 chunk_text(源码)。
    验证:chunk 的 function_name 非空、callers 落进 payload、embed_text 被编码。
    """
    import asyncio as _asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from aigateway_api import code_rag_routes as routes_mod

    # ---- 桩:build_symbol_chunks 产出的 chunk 携带 function_name + callers + embed_text ----
    def _fake_build_symbol_chunks(source_dir, graph_repo_path, ignore_patterns, *, only_files=None, progress_cb=None):
        return [
            {
                "file_path": "auth.py",
                "filename": "auth.py",
                "language": "python",
                "chunk_index": 0,
                "chunk_text": "def login(user):\n    return user\n",
                "embed_text": "function login in auth.py\nsignature: (user)\ncallers: register",
                "start_line": 1,
                "end_line": 2,
                "function_name": "login",
                "class_name": None,
                "callers": ["register"],
                "callees": [],
                "imports": [],
                "signature": "(user)",
                "docstring": "",
            }
        ]

    monkeypatch.setattr(
        "aigateway_core.pipelines.understanding.code_rag.splitter.build_symbol_chunks",
        _fake_build_symbol_chunks,
    )
    monkeypatch.setattr(
        "aigateway_core.pipelines.understanding.code_rag.graph_builder.build_code_graph",
        lambda src, dst: dst,
    )
    monkeypatch.setattr(
        "aigateway_core.pipelines.understanding.code_rag.embedding_router.probe_embedding_dimension",
        lambda model: 4,
    )

    encoded_texts: list[list[str]] = []
    def _fake_encode(model, texts):
        encoded_texts.append(list(texts))
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]
    monkeypatch.setattr(
        "aigateway_core.pipelines.understanding.code_rag.embedding_router.encode_texts",
        _fake_encode,
    )

    # ---- 桩:app.state ----
    from aigateway_core.shared.auth.sqlite_store import SQLiteStore

    app_state = SimpleNamespace()
    app_state.redis_manager = _FakeRedisManager()
    app_state.sqlite_store = SQLiteStore(db_path=str(tmp_path / "tasks_sym.db"))
    qdrant_mgr = _FakeQdrantManager()
    put_resp = MagicMock()
    put_resp.raise_for_status = MagicMock()
    qdrant_mgr._http.put = AsyncMock(return_value=put_resp)
    app_state.qdrant_manager = qdrant_mgr

    graph_repo_path = str(tmp_path / "code_sym")

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
            graph_repo_path=graph_repo_path,
            workspace_path=None,
            cleanup_dirs=[],
        )

    _asyncio.new_event_loop().run_until_complete(_drive())

    # 关键断言:编码的是 embed_text(结构描述),不是 chunk_text(源码)
    assert encoded_texts, "encode_texts 完全没被调用,链路断了"
    encoded = encoded_texts[0]
    assert "function login" in encoded[0], (
        f"编码的应是 embed_text(结构描述),实际收到: {encoded}. "
        "如果源码被打进 embedding,说明重构没生效。"
    )

    # upsert 的 payload 里 function_name / callers / signature 都必须落盘
    upsert_call = qdrant_mgr._http.put.await_args
    body = upsert_call.kwargs["json"] if "json" in upsert_call.kwargs else upsert_call.args[1]
    if not isinstance(body, dict):
        body = {"points": []}
    written_points = body.get("points", [])
    assert written_points, "没有点被写入 Qdrant"
    payload = written_points[0]["payload"]
    assert payload["function_name"] == "login"
    assert payload["callers"] == ["register"]
    assert payload["signature"] == "(user)"

    # 完整 payload 字段集(spec §Payload)运行时落盘 —— 替代
    # test_code_rag_helpers.py::test_code_rag_routes_build_matching_payload_shape
    # 的静态源码字符串核对。缺任一字段说明 payload 组装漏写。
    required_payload_fields = {
        "document_id", "filename", "file_path", "language", "chunk_index",
        "chunk_text", "chunk_type", "function_name", "class_name",
        "start_line", "end_line", "callers", "callees", "imports",
        "signature", "docstring", "embedding_model",
    }
    assert required_payload_fields.issubset(set(payload.keys())), (
        f"payload 缺字段: {required_payload_fields - set(payload.keys())}"
    )


def test_run_code_import_task_batches_encode_calls_at_64_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """运行时验证 embedding 的分批行为(batch_size=64)——替代旧的静态源码字符串核对
    (test_code_rag_routes_batches_embedding_work 只是 grep 'batch_size = 64' 字符串)。

    生成 70 个 chunk(>64),encode_texts 应被调用 2 次:64 + 6;且每个 batch
    单独 upsert 一次(_http.put 被 await 2 次)。如果分批逻辑被破坏成一次性全量 encode,
    这里会 encode 调用次数 = 1 / 单批 70 条 → 测试失败。
    """
    import asyncio as _asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from aigateway_api import code_rag_routes as routes_mod

    TOTAL = 70
    BATCH = 64

    def _fake_build_symbol_chunks(source_dir, graph_repo_path, ignore_patterns, *, only_files=None, progress_cb=None):
        return [
            {
                "file_path": f"mod_{i}.py",
                "filename": f"mod_{i}.py",
                "language": "python",
                "chunk_index": 0,
                "chunk_text": f"def fn_{i}():\n    pass\n",
                "embed_text": f"function fn_{i} in mod_{i}.py",
                "start_line": 1,
                "end_line": 2,
                "function_name": f"fn_{i}",
                "class_name": None,
                "callers": [],
                "callees": [],
                "imports": [],
                "signature": "()",
                "docstring": "",
            }
            for i in range(TOTAL)
        ]

    monkeypatch.setattr(
        "aigateway_core.pipelines.understanding.code_rag.splitter.build_symbol_chunks",
        _fake_build_symbol_chunks,
    )
    monkeypatch.setattr(
        "aigateway_core.pipelines.understanding.code_rag.graph_builder.build_code_graph",
        lambda src, dst: dst,
    )
    monkeypatch.setattr(
        "aigateway_core.pipelines.understanding.code_rag.embedding_router.probe_embedding_dimension",
        lambda model: 4,
    )

    encode_calls: list[list[str]] = []

    def _fake_encode(model, texts):
        encode_calls.append(list(texts))
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    monkeypatch.setattr(
        "aigateway_core.pipelines.understanding.code_rag.embedding_router.encode_texts",
        _fake_encode,
    )

    app_state = SimpleNamespace()
    app_state.redis_manager = _FakeRedisManager()
    from aigateway_core.shared.auth.sqlite_store import SQLiteStore
    app_state.sqlite_store = SQLiteStore(db_path=str(tmp_path / "tasks_batch.db"))
    qdrant_mgr = _FakeQdrantManager()
    put_resp = MagicMock()
    put_resp.raise_for_status = MagicMock()
    qdrant_mgr._http.put = AsyncMock(return_value=put_resp)
    app_state.qdrant_manager = qdrant_mgr

    graph_repo_path = str(tmp_path / "code_batch")

    async def _drive() -> None:
        await routes_mod._run_code_import_task(
            app_state=app_state,
            task_id="t-batch",
            document_id="code_batch",
            source_dir=str(tmp_path),
            source_type="server_path",
            source_label="server_path:///tmp/x",
            embedding_model="Qwen/Qwen3-Embedding-0.6B",
            ignore_patterns=[],
            graph_repo_path=graph_repo_path,
            workspace_path=None,
            cleanup_dirs=[],
        )

    _asyncio.new_event_loop().run_until_complete(_drive())

    # 70 chunks 按 batch_size=64 切:应为 2 批(64 + 6)
    assert len(encode_calls) == 2, (
        f"encode_texts 应被调用 2 次(分批),实际 {len(encode_calls)} 次 —— "
        "若为 1 次说明分批逻辑被破坏成一次性全量"
    )
    assert len(encode_calls[0]) == BATCH
    assert len(encode_calls[1]) == TOTAL - BATCH
    # 每批单独 upsert 一次
    assert qdrant_mgr._http.put.await_count == 2

    # 任务完成后 SQLite 行保留为历史(终态留表);encode/upsert 计数已证明任务正常完成。
    assert app_state.sqlite_store.read_code_rag_task("t-batch")["status"] == "completed"


# ---------------------------------------------------------------------------
# Task 5: SQLite code_rag_tasks store methods
# ---------------------------------------------------------------------------


def test_code_rag_task_upsert_read_list(tmp_path: Path) -> None:
    from aigateway_core.shared.auth.sqlite_store import SQLiteStore

    store = SQLiteStore(db_path=str(tmp_path / "t.db"))
    import time
    now = int(time.time())
    store.upsert_code_rag_task({
        "task_id": "t1", "document_id": "code_abc", "status": "splitting",
        "done": 3, "total": 10, "current_file": "a.py", "source_type": "git",
        "source_label": "https://github.com/x/y.git", "embedding_model": "Qwen",
        "graph_repo_path": "/data/code_graphs/code_abc", "error": "",
        "created_at": now, "updated_at": now,
    })
    row = store.read_code_rag_task("t1")
    assert row is not None
    assert row["status"] == "splitting"
    assert row["done"] == 3
    assert row["source_label"] == "https://github.com/x/y.git"

    # upsert 更新
    store.upsert_code_rag_task({"task_id": "t1", "document_id": "code_abc",
                                 "status": "completed", "done": 10, "total": 10,
                                 "current_file": "", "source_type": "git",
                                 "source_label": "https://github.com/x/y.git",
                                 "embedding_model": "Qwen", "graph_repo_path": "/data/code_graphs/code_abc",
                                 "error": "", "created_at": now, "updated_at": now})
    assert store.read_code_rag_task("t1")["status"] == "completed"

    # list DESC
    store.upsert_code_rag_task({"task_id": "t2", "document_id": "code_def", "status": "pending",
                                 "done": 0, "total": 0, "current_file": "", "source_type": "folder",
                                 "source_label": "folder", "embedding_model": "Qwen", "graph_repo_path": "",
                                 "error": "", "created_at": now + 10, "updated_at": now + 10})
    rows = store.list_code_rag_tasks(limit=50, offset=0)
    assert [r["task_id"] for r in rows] == ["t2", "t1"]


def test_fail_non_terminal_tasks(tmp_path: Path) -> None:
    from aigateway_core.shared.auth.sqlite_store import SQLiteStore
    import time

    store = SQLiteStore(db_path=str(tmp_path / "t.db"))
    now = int(time.time())
    base = {"document_id": "x", "done": 0, "total": 0, "current_file": "", "source_type": "git",
            "source_label": "", "embedding_model": "", "graph_repo_path": "", "error": "",
            "created_at": now, "updated_at": now}
    store.upsert_code_rag_task({**base, "task_id": "a", "status": "splitting"})
    store.upsert_code_rag_task({**base, "task_id": "b", "status": "embedding"})
    store.upsert_code_rag_task({**base, "task_id": "c", "status": "completed"})
    store.upsert_code_rag_task({**base, "task_id": "d", "status": "failed"})

    n = store.fail_non_terminal_tasks("worker restarted")
    assert n == 2
    assert store.read_code_rag_task("a")["status"] == "failed"
    assert store.read_code_rag_task("a")["error"] == "worker restarted"
    assert store.read_code_rag_task("b")["status"] == "failed"
    assert store.read_code_rag_task("c")["status"] == "completed"  # 不动
    assert store.read_code_rag_task("d")["status"] == "failed"  # 已是终态

    # 幂等:再跑一次,0 个非终态
    assert store.fail_non_terminal_tasks("worker restarted") == 0


# ---------------------------------------------------------------------------
# Task 6: task state Redis -> SQLite (routes read SQLite)
# ---------------------------------------------------------------------------


def test_list_code_tasks_reads_sqlite(tmp_path: Path) -> None:
    """list_code_tasks 必须从 app.state.sqlite_store 读 SQLite,而非 Redis。"""
    import time
    import types

    from aigateway_core.shared.auth.sqlite_store import SQLiteStore

    from aigateway_api.code_rag_routes import list_code_tasks

    store = SQLiteStore(db_path=str(tmp_path / "t.db"))
    now = int(time.time())
    store.upsert_code_rag_task({
        "task_id": "t1", "document_id": "d1", "status": "completed",
        "done": 5, "total": 5, "current_file": "", "source_type": "git",
        "source_label": "https://github.com/x/y.git", "embedding_model": "",
        "graph_repo_path": "", "error": "", "created_at": now, "updated_at": now,
    })

    # 路由签名是 list_code_tasks(request, limit, offset, _auth)。
    # request.app.state.sqlite_store 是真正的取用路径,这里构造匹配的桩。
    request = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(sqlite_store=store)))
    out = asyncio.new_event_loop().run_until_complete(
        list_code_tasks(request, 50, 0, {})
    )
    assert len(out) == 1
    assert out[0]["task_id"] == "t1"
    assert out[0]["status"] == "completed"
    # JSON shape 不变 + source_label 原样(git 前缀已在端点层去掉)
    assert out[0]["source_label"] == "https://github.com/x/y.git"


def test_get_code_task_reads_sqlite(tmp_path: Path) -> None:
    """get_code_task 从 SQLite 读;404 行为不变。"""
    import time
    import types

    from aigateway_core.shared.auth.sqlite_store import SQLiteStore

    from aigateway_api.code_rag_routes import get_code_task

    store = SQLiteStore(db_path=str(tmp_path / "t.db"))
    now = int(time.time())
    store.upsert_code_rag_task({
        "task_id": "t2", "document_id": "d2", "status": "splitting",
        "done": 3, "total": 10, "current_file": "a.py", "source_type": "git",
        "source_label": "https://github.com/x/y.git", "embedding_model": "",
        "graph_repo_path": "", "error": "", "created_at": now, "updated_at": now,
    })

    request = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(sqlite_store=store)))
    out = asyncio.new_event_loop().run_until_complete(get_code_task("t2", request, {}))
    assert out["task_id"] == "t2"
    assert out["status"] == "splitting"
    assert out["done"] == 3
    assert out["total"] == 10
    assert out["current_file"] == "a.py"


def test_cancel_code_task_uses_sqlite_cas(tmp_path: Path) -> None:
    """cancel_code_task 走 SQLite 条件 UPDATE,已终态不覆盖。"""
    import time
    import types

    from aigateway_core.shared.auth.sqlite_store import SQLiteStore

    from aigateway_api.code_rag_routes import cancel_code_task

    store = SQLiteStore(db_path=str(tmp_path / "t.db"))
    now = int(time.time())
    base = {
        "document_id": "d3", "done": 0, "total": 0, "current_file": "",
        "source_type": "git", "source_label": "", "embedding_model": "",
        "graph_repo_path": "", "error": "", "created_at": now, "updated_at": now,
    }
    # 一个运行中(splitting) + 一个已 completed
    store.upsert_code_rag_task({**base, "task_id": "running", "status": "splitting"})
    store.upsert_code_rag_task({**base, "task_id": "done", "status": "completed"})

    state = types.SimpleNamespace(sqlite_store=store, code_rag_active_tasks={})
    request = types.SimpleNamespace(app=types.SimpleNamespace(state=state))
    # 取消运行中任务
    out = asyncio.new_event_loop().run_until_complete(cancel_code_task("running", request, {}))
    assert out["status"] == "cancelled"
    assert store.read_code_rag_task("running")["status"] == "cancelled"

    # 取消已终态任务 → 不覆盖,返回当前状态
    out2 = asyncio.new_event_loop().run_until_complete(cancel_code_task("done", request, {}))
    assert out2["status"] == "completed"
    assert store.read_code_rag_task("done")["status"] == "completed"


def test_import_code_repository_writes_sqlite_and_strips_git_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """import 端点:pending 落 SQLite(非 Redis);git source_label 无 git:// 前缀。"""
    from aigateway_core.shared.auth.sqlite_store import SQLiteStore

    store = SQLiteStore(db_path=str(tmp_path / "t.db"))
    client, app = _make_client(monkeypatch)
    # 把 sqlite_store 挂上(模拟 main.py lifespan 做的事)
    app.state.sqlite_store = store
    # 避免真的跑 git clone(gitpython 在测试环境没装 / 不应触网)
    fake_src = tmp_path / "git_src"
    fake_src.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "aigateway_api.code_rag_routes._materialize_git_repo",
        lambda url, branch, *, dest_dir=None: str(fake_src),
    )

    response = client.post(
        "/admin/rag/code/import",
        json={
            "source_type": "git",
            "git_url": "https://github.com/octocat/Hello-World.git",
            "embedding_model": "Qwen/Qwen3-Embedding-0.6B",
        },
    )
    assert response.status_code == 200, response.text
    task_id = response.json()["task_id"]

    row = store.read_code_rag_task(task_id)
    assert row is not None, "import 端点没把 task 写入 SQLite"
    assert row["status"] == "pending"
    assert row["source_type"] == "git"
    # git:// 前缀已去掉,前端直接拿到 URL
    assert row["source_label"] == "https://github.com/octocat/Hello-World.git"
    assert (row["document_id"] or "").startswith("code_")


# ---------------------------------------------------------------------------
# Task 7: startup orphan task sweep
# ---------------------------------------------------------------------------


def test_sweep_orphaned_tasks_marks_non_terminal(tmp_path: Path) -> None:
    """sweep_orphaned_tasks 把非终态任务标 failed,清理孤儿临时目录。"""
    import glob
    import time
    import types

    from aigateway_core.shared.auth.sqlite_store import SQLiteStore

    from aigateway_api.code_rag_routes import sweep_orphaned_tasks

    store = SQLiteStore(db_path=str(tmp_path / "t.db"))
    now = int(time.time())
    base = {
        "document_id": "x", "done": 0, "total": 0, "current_file": "",
        "source_type": "git", "source_label": "", "embedding_model": "",
        "graph_repo_path": "", "error": "", "created_at": now, "updated_at": now,
    }
    store.upsert_code_rag_task({**base, "task_id": "a", "status": "splitting"})
    store.upsert_code_rag_task({**base, "task_id": "b", "status": "completed"})

    # 造一个孤儿临时目录(模拟旧的 /tmp/code_rag_folder_* 残留)
    orphan = tempfile.mkdtemp(prefix="code_rag_folder_")
    assert Path(orphan).exists()

    app_state = types.SimpleNamespace(sqlite_store=store)
    n = sweep_orphaned_tasks(app_state)
    assert n == 1
    assert store.read_code_rag_task("a")["status"] == "failed"
    assert "worker restarted" in store.read_code_rag_task("a")["error"]
    assert store.read_code_rag_task("b")["status"] == "completed"

    # 孤儿临时目录已被清掉
    assert not Path(orphan).exists(), f"sweep 未清理孤儿临时目录: {orphan}"

    # 幂等:再扫一次,0 个非终态
    assert sweep_orphaned_tasks(app_state) == 0


def test_sweep_orphaned_tasks_no_store_returns_zero() -> None:
    """sqlite_store 缺失时返回 0(不崩)。"""
    import types

    from aigateway_api.code_rag_routes import sweep_orphaned_tasks

    app_state = types.SimpleNamespace()
    assert sweep_orphaned_tasks(app_state) == 0


# ---------------------------------------------------------------------------
# Task 8: splitting progress callback wired to SQLite
# ---------------------------------------------------------------------------


def test_splitting_progress_written_to_sqlite(tmp_path: Path) -> None:
    """build_symbol_chunks 的 progress_cb 经 run_coroutine_threadsafe 回写 SQLite。

    splitter 跑在 executor 线程,progress_cb 必须把 _mark(done,total,current_file)
    调度回主 event loop 写 SQLite。空 chunks → completed,但 total 应为最后一次回写值。
    """
    import asyncio as _aio
    import time
    import types

    from aigateway_core.shared.auth.sqlite_store import SQLiteStore

    import aigateway_api.code_rag_routes as routes
    import aigateway_core.pipelines.understanding.code_rag.splitter as splitter_mod

    store = SQLiteStore(db_path=str(tmp_path / "t.db"))
    now = int(time.time())
    store.upsert_code_rag_task({
        "task_id": "t1", "document_id": "d1", "status": "splitting",
        "done": 0, "total": 0, "current_file": "", "source_type": "git",
        "source_label": "", "embedding_model": "", "graph_repo_path": "",
        "error": "", "created_at": now, "updated_at": now,
    })
    app_state = types.SimpleNamespace(
        sqlite_store=store, redis_manager=None, qdrant_manager=None, config_manager=None,
    )

    def fake_build(source_dir, graph_repo_path, ignore_patterns, *, only_files=None, progress_cb=None):
        if progress_cb:
            progress_cb(done=5, total=10, current_file="a.py")
            progress_cb(done=10, total=10, current_file="b.py")
        return []

    orig = splitter_mod.build_symbol_chunks
    splitter_mod.build_symbol_chunks = fake_build
    import aigateway_core.pipelines.understanding.code_rag.graph_builder as graph_builder_mod
    orig_build_graph = graph_builder_mod.build_code_graph
    graph_builder_mod.build_code_graph = lambda src, dst: dst
    try:
        loop = _aio.new_event_loop()
        loop.run_until_complete(routes._run_code_import_task(
            app_state=app_state, task_id="t1", document_id="d1", source_dir="/x",
            source_type="git", source_label="", embedding_model="Qwen",
            ignore_patterns=[], graph_repo_path="/x", workspace_path=None,
            cleanup_dirs=[],
        ))
    finally:
        splitter_mod.build_symbol_chunks = orig
        graph_builder_mod.build_code_graph = orig_build_graph

    row = store.read_code_rag_task("t1")
    # 空 chunks → completed;分片阶段至少回写过 total=10
    assert row["total"] == 10
    assert row["status"] == "completed"
