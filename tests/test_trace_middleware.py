"""TraceMiddleware 测试 —— 生成 trace_id、写 request.state、回写响应头."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

import asyncio
from unittest.mock import AsyncMock, MagicMock

from starlette.testclient import TestClient
from fastapi import FastAPI, Request

from aigateway_api.trace_middleware import TraceMiddleware


def _make_app(redis_mock=None):
    app = FastAPI()
    if redis_mock is not None:
        # 中间件从 scope["app"].state.redis_manager.redis 读取 Redis 客户端
        app.state.redis_manager = AsyncMock()
        app.state.redis_manager.redis = redis_mock
    app.add_middleware(TraceMiddleware)

    @app.get("/ping")
    async def ping(request: Request):
        return {"trace_id": request.state.trace_id}

    return app


def test_middleware_generates_trace_id_when_absent():
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/ping")
    assert resp.status_code == 200
    tid = resp.json()["trace_id"]
    assert len(tid) == 32  # uuid4().hex
    # 响应头回写
    assert resp.headers["x-trace-id"] == tid


def test_middleware_uses_incoming_x_trace_id():
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/ping", headers={"x-trace-id": "incoming-id-123"})
    assert resp.json()["trace_id"] == "incoming-id-123"
    assert resp.headers["x-trace-id"] == "incoming-id-123"


def test_middleware_flushes_to_redis():
    redis_mock = AsyncMock()
    redis_mock.hset = AsyncMock()
    redis_mock.expire = AsyncMock()
    app = _make_app(redis_mock=redis_mock)
    client = TestClient(app)
    resp = client.get("/ping")
    assert resp.status_code == 200
    # flush 被调用
    assert redis_mock.hset.called
    args = redis_mock.hset.call_args.args
    assert args[0].startswith("aigateway:trace:")
    assert redis_mock.expire.called
