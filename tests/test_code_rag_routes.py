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


def test_get_code_task_returns_default_shape_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _make_client(monkeypatch)
    response = client.get("/admin/rag/code/tasks/does-not-exist")
    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == "does-not-exist"
    assert body["status"] == "pending"
    assert body["done"] == 0
    assert body["total"] == 0


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
