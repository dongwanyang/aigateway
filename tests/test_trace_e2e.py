"""端到端: 单次 HTTP 请求全链路 trace_id 一致性 + collector 落 Redis."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

from unittest.mock import AsyncMock
from starlette.testclient import TestClient
from fastapi import FastAPI, Request

from aigateway_api.trace_middleware import TraceMiddleware
from aigateway_core.shared.trace_event import TraceCollector


def test_single_request_one_trace_id():
    """一次请求只生成一个 trace_id, 所有事件归属同一 collector."""
    app = FastAPI()
    # 中间件从 scope["app"].state.redis_manager.redis 读取 Redis 客户端,
    # 所以需要在 redis_manager 上挂 mock。
    redis_mock = AsyncMock()
    redis_mock.hset = AsyncMock()
    redis_mock.expire = AsyncMock()
    app.state.redis_manager = AsyncMock()
    app.state.redis_manager.redis = redis_mock
    app.add_middleware(TraceMiddleware)

    captured = {}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        collector = TraceCollector.current()
        captured["trace_id"] = request.state.trace_id
        captured["collector_trace_id"] = collector.trace_id if collector else None
        return {"id": "chatcmpl-1", "choices": []}

    client = TestClient(app)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    # request.state.trace_id 和 collector.trace_id 一致
    assert captured["trace_id"] == captured["collector_trace_id"]
    # 响应头回写 X-Trace-Id
    assert resp.headers["x-trace-id"] == captured["trace_id"]
    # flush 写 Redis, key 用同一 trace_id
    assert redis_mock.hset.called
    key = redis_mock.hset.call_args.args[0]
    assert key == f"aigateway:trace:{captured['trace_id']}"


def test_incoming_x_trace_id_preserved_end_to_end():
    """入站 X-Trace-Id 头透传到 collector 与响应头."""
    app = FastAPI()
    redis_mock = AsyncMock()
    redis_mock.hset = AsyncMock()
    redis_mock.expire = AsyncMock()
    app.state.redis_manager = AsyncMock()
    app.state.redis_manager.redis = redis_mock
    app.add_middleware(TraceMiddleware)

    @app.get("/ping")
    async def ping(request: Request):
        return {"trace_id": request.state.trace_id}

    client = TestClient(app)
    resp = client.get("/ping", headers={"x-trace-id": "e2e-inbound-42"})
    assert resp.status_code == 200
    assert resp.json()["trace_id"] == "e2e-inbound-42"
    assert resp.headers["x-trace-id"] == "e2e-inbound-42"
