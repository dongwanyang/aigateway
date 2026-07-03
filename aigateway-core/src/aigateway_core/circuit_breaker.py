"""
CircuitBreaker — 熔断器
=====================

per-provider 独立的熔断状态机: CLOSED(0) / OPEN(1) / HALF-OPEN(2)。

根据 DB_SCHEMA.md §3 熔断器状态定义:
- CLOSED (0): 正常操作
- OPEN (1): 拒绝所有请求，立即触发降级
- HALF-OPEN (2): 放行一个探测请求

结合 TECH_SPEC.md:
- pybreaker 1.1+ 轻量级 Circuit Breaker
- failure_threshold 默认 5
- recovery_timeout 默认 60 秒
"""

from __future__ import annotations

import enum
import logging
import time
from typing import Any, Dict, Optional, Type

from .exceptions import CircuitBreakerOpenError

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 熔断器状态枚举
# ------------------------------------------------------------------


class CircuitState(enum.IntEnum):
    """熔断器状态。

    DB_SCHEMA.md 定义的三态:
    - CLOSED (0): 正常操作
    - OPEN (1): 拒绝所有请求
    - HALF_OPEN (2): 放行探测请求
    """

    CLOSED = 0
    OPEN = 1
    HALF_OPEN = 2


# ------------------------------------------------------------------
# 熔断器
# ------------------------------------------------------------------


class CircuitBreaker:
    """per-provider 熔断器实例。

    状态转换规则:
    - CLOSED -> OPEN: 连续失败次数 >= failure_threshold
    - OPEN -> HALF_OPEN: 经过 recovery_timeout 后自动转换
    - HALF_OPEN -> CLOSED: 探测请求成功
    - HALF_OPEN -> OPEN: 探测请求失败

    属性:
        provider: 提供商名称，如 "openai"。
        failure_threshold: 连续失败阈值，默认 5。
        recovery_timeout: HALF_OPEN 等待恢复时间（秒），默认 60。
        expected_exception: 触发熔断的异常类型。
        state: 当前状态。
        failure_count: 当前连续失败次数。
        last_failure_time: 最后一次失败的时间戳。
    """

    def __init__(
        self,
        provider: str,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        expected_exception: Optional[Type[Exception]] = None,
    ) -> None:
        """
        Args:
            provider: 提供商名称，如 "openai"。
            failure_threshold: 连续失败次数触发 OPEN，默认 5。
            recovery_timeout: 从 OPEN 到 HALF_OPEN 的等待时间（秒），默认 60。
            expected_exception: 触发熔断的异常类型，
                                默认 litellm.BadRequestError（未导入时 fallback 到 Exception）。
        """
        self.provider = provider
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout

        # 期望捕获的异常类型
        if expected_exception is None:
            # 尝试导入 litellm 的特定异常
            try:
                import litellm
                expected_exception = litellm.BadRequestError  # type: ignore[assignment]
            except ImportError:
                expected_exception = Exception

        self.expected_exception = expected_exception

        # 状态
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: float = 0.0
        self.last_success_time: float = 0.0
        self._last_transition_time: float = time.time()

        # 保护锁（线程安全）
        import threading
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    @property
    def is_closed(self) -> bool:
        """是否为 CLOSED 状态（正常工作）。"""
        return self.state == CircuitState.CLOSED

    @property
    def is_open(self) -> bool:
        """是否为 OPEN 状态（拒绝请求）。"""
        return self.state == CircuitState.OPEN

    @property
    def is_half_open(self) -> bool:
        """是否为 HALF-OPEN 状态（探测中）。"""
        return self.state == CircuitState.HALF_OPEN

    def get_state_value(self) -> int:
        """获取状态的整数值，用于 Prometheus 指标上报。

        Returns:
            0=CLOSED, 1=OPEN, 2=HALF-OPEN。
        """
        return int(self.state)

    # ------------------------------------------------------------------
    # 请求决策
    # ------------------------------------------------------------------

    def allow_request(self) -> bool:
        """判断是否允许发出请求。

        CLOSED 状态：始终允许。
        OPEN 状态：检查是否已过 recovery_timeout，若是则转入 HALF_OPEN 并允许。
        HALF_OPEN 状态：仅允许一个探测请求（简化实现：直接允许）。

        Returns:
            是否允许请求。
        """
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True

            if self.state == CircuitState.OPEN:
                # 检查是否过了恢复超时
                elapsed = time.time() - self.last_failure_time
                if elapsed >= self.recovery_timeout:
                    self._transition_state(CircuitState.OPEN, CircuitState.HALF_OPEN)
                    return True
                return False

            # HALF_OPEN: 允许一个探测请求
            return True

    def record_success(self) -> None:
        """记录一次成功的请求。

        HALF_OPEN -> CLOSED（探测成功）。
        CLOSED -> 重置失败计数。
        """
        with self._lock:
            self.last_success_time = time.time()

            if self.state == CircuitState.HALF_OPEN:
                self._transition_state(CircuitState.HALF_OPEN, CircuitState.CLOSED)
                self.failure_count = 0
            elif self.state == CircuitState.CLOSED:
                self.failure_count = 0

    def record_failure(self) -> None:
        """记录一次失败的请求。

        CLOSED -> OPEN（失败计数达标）。
        HALF_OPEN -> OPEN（探测失败）。
        """
        with self._lock:
            self.last_failure_time = time.time()
            self.failure_count += 1

            if self.state == CircuitState.HALF_OPEN:
                # 探测请求也失败了，回到 OPEN
                self._transition_state(CircuitState.HALF_OPEN, CircuitState.OPEN)
            elif self.state == CircuitState.CLOSED:
                if self.failure_count >= self.failure_threshold:
                    self._transition_state(CircuitState.CLOSED, CircuitState.OPEN)

    def _transition_state(self, from_state: CircuitState, to_state: CircuitState) -> None:
        """处理状态转换，记录日志并上报指标。

        Args:
            from_state: 当前状态。
            to_state: 目标状态。
        """
        self.state = to_state
        self._last_transition_time = time.time()

        # 上报 Prometheus 指标
        try:
            from .metrics import get_metrics_collector
            metrics = get_metrics_collector()
            metrics.set_circuit_breaker_state(self.provider, int(to_state))
        except Exception:
            pass

        # 记录日志（不同级别）
        if to_state == CircuitState.OPEN:
            logger.error(
                "熔断器 %s: %s -> OPEN (连续失败 %d 次)",
                self.provider, from_state.name, self.failure_count,
            )
        elif to_state == CircuitState.HALF_OPEN:
            logger.info(
                "熔断器 %s: %s -> HALF_OPEN",
                self.provider, from_state.name,
            )
        elif to_state == CircuitState.CLOSED:
            logger.info(
                "熔断器 %s: %s -> CLOSED (恢复正常)",
                self.provider, from_state.name,
            )

    def check_long_open(self, threshold_seconds: int = 300) -> bool:
        """检查熔断器是否持续 OPEN 超过阈值时间。

        Args:
            threshold_seconds: OPEN 状态持续时间阈值（秒），默认 300（5分钟）。

        Returns:
            是否超过阈值。
        """
        if self.state == CircuitState.OPEN:
            duration = time.time() - self._last_transition_time
            if duration >= threshold_seconds:
                try:
                    from .metrics import get_metrics_collector
                    metrics = get_metrics_collector()
                    # 通过记录计数器指标来告警
                    if metrics._requests_counter:  # 简单检查是否初始化
                        metrics.set_circuit_breaker_state(self.provider, 1)
                except Exception:
                    pass
                return True
        return False

    # ------------------------------------------------------------------
    # 装饰器 — 自动熔断
    # ------------------------------------------------------------------

    def protect(self, func: Any) -> Any:
        """装饰器：自动管理熔断状态。

        用法:
            @cb.protect
            async def call_llm(): ...

        调用逻辑:
        1. 检查 allow_request()
        2. 调用原函数
        3. 成功 -> record_success()
        4. 失败（预期异常）-> record_failure() + 抛出 CircuitBreakerOpenError
        """
        import functools

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not self.allow_request():
                raise CircuitBreakerOpenError(
                    self.provider, self.state.name
                )

            try:
                result = await func(*args, **kwargs)
                self.record_success()
                return result
            except self.expected_exception as exc:  # type: ignore[arg-type]
                self.record_failure()
                logger.warning(
                    "熔断器 %s 记录失败: %s",
                    self.provider,
                    exc,
                )
                raise CircuitBreakerOpenError(
                    self.provider, self.state.name
                ) from exc
            except Exception as exc:
                # 非预期异常也记录，但不触发熔断
                logger.error(
                    "熔断器 %s 捕获非预期异常: %s",
                    self.provider,
                    exc,
                )
                raise

        return wrapper

    # ------------------------------------------------------------------
    # 手动控制
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """手动重置熔断器为 CLOSED 状态。"""
        with self._lock:
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            self.last_failure_time = 0.0
            self.last_success_time = time.time()
            logger.info("熔断器 %s: 手动重置为 CLOSED", self.provider)

    def force_open(self) -> None:
        """强制设置为 OPEN 状态。

        用于运维主动熔断某个不可用的提供商。
        """
        with self._lock:
            self.state = CircuitState.OPEN
            self.last_failure_time = time.time()
            logger.warning("熔断器 %s: 强制设为 OPEN", self.provider)

    def get_status(self) -> Dict[str, Any]:
        """获取熔断器的完整状态快照（用于健康检查 / 指标）。

        Returns:
            状态字典，包含 last_transition_time 和 open_duration_seconds。
        """
        now = time.time()
        open_duration = None
        if self.state == CircuitState.OPEN:
            open_duration = now - self._last_transition_time

        return {
            "provider": self.provider,
            "state": self.state.name,
            "state_value": int(self.state),
            "failure_count": self.failure_count,
            "failure_threshold": self.failure_threshold,
            "last_failure_time": self.last_failure_time,
            "last_success_time": self.last_success_time,
            "last_transition_time": self._last_transition_time,
            "open_duration_seconds": open_duration,
        }


# ------------------------------------------------------------------
# 熔断器工厂
# ------------------------------------------------------------------


class CircuitBreakerFactory:
    """per-provider 熔断器工厂。

    为每个提供商创建和维护独立的熔断器实例。

    受影响的提供商枚举: openai, anthropic, gemini, bedrock, ollama
    """

    # 默认熔断器配置
    DEFAULT_FAILURE_THRESHOLD = 5
    DEFAULT_RECOVERY_TIMEOUT = 60

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        long_open_alert_seconds: int = 300,
    ) -> None:
        self._breakers: Dict[str, CircuitBreaker] = {}
        self.DEFAULT_FAILURE_THRESHOLD = failure_threshold
        self.DEFAULT_RECOVERY_TIMEOUT = recovery_timeout
        self.long_open_alert_seconds = long_open_alert_seconds

    def get_or_create(
        self,
        provider: str,
        failure_threshold: Optional[int] = None,
        recovery_timeout: Optional[int] = None,
    ) -> CircuitBreaker:
        """获取或创建一个 per-provider 熔断器。

        Args:
            provider: 提供商名称。
            failure_threshold: 失败阈值，默认 5。
            recovery_timeout: 恢复超时（秒），默认 60。

        Returns:
            CircuitBreaker 实例。
        """
        if provider not in self._breakers:
            self._breakers[provider] = CircuitBreaker(
                provider=provider,
                failure_threshold=failure_threshold or self.DEFAULT_FAILURE_THRESHOLD,
                recovery_timeout=recovery_timeout or self.DEFAULT_RECOVERY_TIMEOUT,
            )
            logger.info(
                "熔断器工厂: 创建新实例 provider=%s",
                provider,
            )

        return self._breakers[provider]

    def get(self, provider: str) -> Optional[CircuitBreaker]:
        """获取指定提供商的熔断器。

        Args:
            provider: 提供商名称。

        Returns:
            熔断器实例，不存在则返回 None。
        """
        return self._breakers.get(provider)

    def all_status(self) -> Dict[str, Dict[str, Any]]:
        """获取所有熔断器的状态快照。

        Returns:
            {provider_name: status_dict}。
        """
        return {name: cb.get_status() for name, cb in self._breakers.items()}

    def reset_all(self) -> None:
        """重置所有熔断器。"""
        for breaker in self._breakers.values():
            breaker.reset()
        logger.info("熔断器工厂: 全部重置")
