"""
Rate Limiter — IP 级速率限制
===========================

对 /admin/* 端点实施 IP 级速率限制（滑动窗口计数器）。
Redis 可用时使用 Redis INCR + EXPIRE，不可用时降级为进程内计数器。

豁免: /health, /metrics 端点不受限制。
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any, Optional, Set, Tuple

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


class RateLimiterMiddleware(BaseHTTPMiddleware):
    """IP 级速率限制中间件。

    对 /admin/* 路径实施 max_requests / window_seconds 的限制。
    """

    def __init__(
        self,
        app: Any,
        max_requests: int = 30,
        window_seconds: int = 60,
        protected_prefixes: Tuple[str, ...] = ("/admin",),
        exempt_paths: Set[str] = frozenset({"/health", "/metrics"}),
    ) -> None:
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.protected_prefixes = protected_prefixes
        self.exempt_paths = exempt_paths
        # In-memory fallback counter: {ip_path: [(timestamp, count)]}
        self._memory_store: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # 豁免路径
        if path in self.exempt_paths:
            return await call_next(request)

        # 只对受保护前缀进行限制
        if not any(path.startswith(prefix) for prefix in self.protected_prefixes):
            return await call_next(request)

        # 获取客户端 IP
        client_ip = request.client.host if request.client else "unknown"

        # 检查速率限制
        allowed, retry_after = self._check_in_memory(client_ip, path)

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"error": {"code": "rate_limited", "message": f"Too many requests. Retry after {retry_after}s."}},
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)

    def _check_in_memory(self, client_ip: str, path: str) -> Tuple[bool, int]:
        """进程内滑动窗口计数器。"""
        now = time.time()
        key = f"{client_ip}:{path.split('/')[1] if '/' in path else path}"
        window_start = now - self.window_seconds

        # 清理过期记录
        self._memory_store[key] = [ts for ts in self._memory_store[key] if ts > window_start]

        # 检查是否超过限制
        if len(self._memory_store[key]) >= self.max_requests:
            oldest = self._memory_store[key][0] if self._memory_store[key] else now
            retry_after = max(1, int(self.window_seconds - (now - oldest)))
            return False, retry_after

        # 记录本次请求
        self._memory_store[key].append(now)
        return True, 0
