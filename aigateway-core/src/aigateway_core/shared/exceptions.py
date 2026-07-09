"""
Gateway 异常层次定义
==================

TECH_SPEC.md 定义的异常继承树:
    GatewayError
    ├── AuthError
    ├── QuotaExceededError
    └── CircuitBreakerOpenError

拆分到此独立模块以避免 security.py 与 circuit_breaker.py 之间的循环导入。
"""

from __future__ import annotations


class GatewayError(Exception):
    """Gateway 基类异常。"""

    pass


class AuthError(GatewayError):
    """API Key 认证失败。"""

    pass


class QuotaExceededError(GatewayError):
    """配额耗尽异常。"""

    def __init__(self, message: str, retry_after: int = 0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class CircuitBreakerOpenError(GatewayError):
    """熔断器 OPEN 时抛出。"""

    def __init__(self, provider: str, state: str = "OPEN") -> None:
        self.provider = provider
        self.state = state
        super().__init__(
            f"Circuit breaker OPEN for provider '{provider}'"
        )
