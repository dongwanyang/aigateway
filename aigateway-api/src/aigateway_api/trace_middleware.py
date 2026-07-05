"""ASGI 中间件:统一生成/复用 trace_id,启动 TraceCollector,响应回写 X-Trace-Id.

修"一次请求 3+ mint 点"bug 的核心:所有下游代码(ctx 构造、log-recorder、
插件)都从 request.state.trace_id 或 TraceCollector.current() 取同一个 id,
不再各自 uuid4()。
"""
from __future__ import annotations

import uuid
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from aigateway_core.trace_event import TraceCollector


class TraceMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 从请求头读 trace_id,没有就生成
        headers = dict(scope.get("headers", []))
        trace_id = (
            headers.get(b"x-trace-id", b"").decode("ascii")
            or uuid.uuid4().hex
        )

        # 写 scope["state"](FastAPI request.state 的底层)
        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["trace_id"] = trace_id

        # 启动 collector(写 ContextVar)
        collector = TraceCollector.start(trace_id)

        # 拿 redis 用于 flush(scope["state"] 在 app.state 之外)
        # app.state.redis_manager 在 lifespan 里设置
        app_obj = scope.get("app")
        redis_mgr = getattr(app_obj.state, "redis_manager", None) if app_obj else None
        redis_client = getattr(redis_mgr, "redis", None) if redis_mgr else None

        status_holder = {"status": 500}

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                # 回写 X-Trace-Id 响应头
                headers_list = list(message.get("headers", []))
                headers_list.append((b"x-trace-id", trace_id.encode("ascii")))
                message["headers"] = headers_list
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            try:
                await collector.flush(redis_client)
            except Exception:
                # flush 失败不影响请求
                pass
