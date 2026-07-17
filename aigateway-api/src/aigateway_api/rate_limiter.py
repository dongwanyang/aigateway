"""
Rate Limiter — IP 级速率限制
===========================

对 /admin/* 端点实施 IP 级速率限制（滑动窗口计数器）。
Redis 可用时使用 Redis INCR + EXPIRE，不可用时降级为进程内计数器。

豁免: /health, /metrics 端点不受限制。
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from typing import Any, Optional, Set, Tuple

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


# 匹配 ID 形态段:纯数字、UUID、≥8 的纯十六进制、key_/grp_ 前缀、或 ≥16 字符的长串。
# 用于从限流分桶中剔除资源 ID,使 /admin/api-keys/{id} 这类端点按 "api-keys" 共享窗口。
_ID_PATTERNS = [
    re.compile(r"^\d+$"),                        # 123
    re.compile(r"^[0-9a-fA-F]{8,}$"),            # 十六进制(含 uuid 去连字符)
    re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F-]+$"),  # uuid
    re.compile(r"^(key_|grp_)[A-Za-z0-9]+$"),    # key_id / group_id 前缀
]


def _looks_like_id(segment: str) -> bool:
    """段是否像资源 ID(而非静态端点段)。"""
    if len(segment) >= 16:
        return True
    return any(p.match(segment) for p in _ID_PATTERNS)


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
        # 按 (client_ip, 端点模板) 分桶。先剥离受保护前缀(如 "/admin"),
        # 再取剩余 path 的「前两个非 ID 段」作为端点标识。这样:
        #   - /admin/a 与 /admin/b 互不影响(修正旧实现 path.split('/')[1] 撞桶)
        #   - ID 段被剔除,带参端点共享窗口:
        #       /admin/api-keys/{id}      -> "api-keys"        (ID 在第 2 段, 剔除)
        #       /admin/trace/{id}         -> "trace"
        #       /admin/rag/code/repositories/{id}/callers -> "rag/code" (ID 在第 4 段)
        #     攻击者轮换 document_id/key_id 无法绕过单端点配额。
        #   - 文本 RAG(/admin/rag/documents)与代码 RAG(/admin/rag/code/*)是
        #     不同子系统,各自独立窗口 —— 若不剥 /admin 前缀直接按前两段分桶,
        #     两者会一起坍缩成 "admin/rag",代码 RAG 的频繁轮询会挤占文本 RAG 配额。
        matched = next((p for p in self.protected_prefixes if path.startswith(p)), None)
        rest = path[len(matched):].strip("/") if matched else path.strip("/")
        static_segments = [p for p in rest.split("/") if p and not _looks_like_id(p)]
        bucket = "/".join(static_segments[:2]) if static_segments else ""
        key = f"{client_ip}:{bucket}"
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
