"""ASGI middleware for stable request and trace identities."""
from __future__ import annotations

import re
import uuid
from typing import Any

from aigateway_core.shared.trace_event import TraceCollector
from starlette.types import ASGIApp, Receive, Scope, Send

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _header_text(headers: dict[bytes, bytes], name: bytes) -> str:
    try:
        return headers.get(name, b"").decode("ascii").strip()
    except UnicodeDecodeError:
        return ""


def _validated_request_id(value: str) -> str:
    return value if _REQUEST_ID_RE.fullmatch(value) else ""


class TraceMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        request_id = (
            _validated_request_id(_header_text(headers, b"x-request-id"))
            or uuid.uuid4().hex
        )
        trace_id = (
            _validated_request_id(_header_text(headers, b"x-trace-id"))
            or request_id
        )

        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["request_id"] = request_id
        scope["state"]["trace_id"] = trace_id

        collector = TraceCollector.start(trace_id)

        app_obj = scope.get("app")
        redis_mgr = getattr(app_obj.state, "redis_manager", None) if app_obj else None
        redis_client = getattr(redis_mgr, "redis", None) if redis_mgr else None

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers_list = list(message.get("headers", []))
                headers_list.append((b"x-request-id", request_id.encode("ascii")))
                headers_list.append((b"x-trace-id", trace_id.encode("ascii")))
                message["headers"] = headers_list
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            try:
                await collector.flush(redis_client)
            except Exception:
                pass
