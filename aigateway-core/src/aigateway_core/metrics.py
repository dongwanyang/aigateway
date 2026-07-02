"""
Metrics — Prometheus 指标采集
============================

暴露以下指标（见 API_CONTRACT.md GET /metrics 定义）:

| 指标名 | 类型 | 说明 |
|--------|------|------|
| gateway_http_requests_total | counter | 总 HTTP 请求数，按 method/endpoint/status 分桶 |
| gateway_request_duration_seconds | histogram | 请求持续时间（秒），按 endpoint 分桶 |
| gateway_cache_hits_total | counter | 缓存命中数，按 tier 分桶 (L1/L2/L3) |
| gateway_cache_misses_total | counter | 缓存未命中数 |
| gateway_tokens_total | counter | 总 token 数，按 type 分桶 (prompt/completion) |
| gateway_cost_total | gauge | 总成本（美元） |
| gateway_cost_by_model | counter | 各模型成本 |
| gateway_circuit_breaker_state | gauge | 各提供商熔断器状态 (0=CLOSED, 1=OPEN, 2=HALF-OPEN) |
| gateway_active_requests | gauge | 当前活跃请求数 |
| gateway_up | gauge | 服务健康状态 (1=up, 0=down) |

根据 TECH_SPEC.md:
- Prometheus Client 0.20+
- Python 原生 SDK，轻量级
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 指标实例缓存
# ------------------------------------------------------------------

_metrics_initialized = False


def _ensure_initialized() -> None:
    """确保 Prometheus SDK 已初始化。

    懒加载，在第一次创建指标时加载 prometheus_client。
    单 worker 模式，直接使用默认 CollectorRegistry。
    """
    global _metrics_initialized
    if _metrics_initialized:
        return

    try:
        from prometheus_client import Counter, Histogram, Gauge, CollectorRegistry
    except ImportError:
        logger.warning(
            "prometheus-client 未安装，指标功能不可用。"
            "请执行: pip install prometheus-client"
        )
        return

    # 单 worker 模式：直接使用默认 registry
    _registry = CollectorRegistry()
    globals()["__registry"] = _registry
    _metrics_initialized = True


# ------------------------------------------------------------------
# 指标管理类
# ------------------------------------------------------------------


class MetricsCollector:
    """Prometheus 指标收集器。

    管理所有 Gateway 指标的定义、更新和查询。
    所有指标通过 prometheus_client 库创建和暴露。

    属性:
        enabled: 是否启用指标采集。
        _start_time: 服务启动时间戳，用于 uptime 计算。
    """

    def __init__(self, enabled: bool = True) -> None:
        """
        Args:
            enabled: 是否启用指标采集，默认 True。
        """
        self.enabled = enabled
        self._start_time = time.time()

        # 缓存指标引用（避免每次创建）
        self._requests_counter: Any = None
        self._duration_histogram: Any = None
        self._cache_hits_counter: Any = None
        self._cache_misses_counter: Any = None
        self._tokens_counter: Any = None
        self._tokens_saved_counter: Any = None
        self._cost_total_gauge: Any = None
        self._cost_by_model_counter: Any = None
        self._cost_by_user_counter: Any = None
        self._circuit_breaker_gauge: Any = None
        self._active_requests_gauge: Any = None
        self._up_gauge: Any = None
        self._registry: Any = None  # prometheus_client.CollectorRegistry

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """初始化所有 Prometheus 指标。

        使用懒加载策略，首次调用时创建底层 Counter/Histogram/Gauge 对象。
        单 worker 模式，使用默认 CollectorRegistry。
        """
        if not self.enabled:
            logger.info("Prometheus 指标已禁用")
            return

        _ensure_initialized()

        from prometheus_client import Counter, Histogram, Gauge

        # 获取单 worker 模式的 registry
        registry = globals().get("__registry")
        self._registry = registry  # 保存引用供 /metrics 端点使用

        # gateway_http_requests_total — counter
        self._requests_counter = Counter(
            "gateway_http_requests_total",
            "Total number of HTTP requests",
            labelnames=["method", "endpoint", "status"],
            registry=registry,
        )

        # gateway_request_duration_seconds — histogram
        self._duration_histogram = Histogram(
            "gateway_request_duration_seconds",
            "Request duration in seconds",
            labelnames=["endpoint"],
            buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
            registry=registry,
        )

        # gateway_cache_hits_total — counter
        self._cache_hits_counter = Counter(
            "gateway_cache_hits_total",
            "Total cache hits by tier",
            labelnames=["tier"],
            registry=registry,
        )

        # gateway_cache_misses_total — counter
        self._cache_misses_counter = Counter(
            "gateway_cache_misses_total",
            "Total cache misses",
            registry=registry,
        )

        # gateway_tokens_total — counter
        self._tokens_counter = Counter(
            "gateway_tokens_total",
            "Total tokens processed",
            labelnames=["type"],
            registry=registry,
        )

        # gateway_tokens_saved_total — counter
        self._tokens_saved_counter = Counter(
            "gateway_tokens_saved",
            "Total tokens saved by cache hits",
            registry=registry,
        )

        # gateway_cost_total — gauge
        self._cost_total_gauge = Gauge(
            "gateway_cost_total",
            "Total cost in USD",
            registry=registry,
        )

        # gateway_cost_by_model — counter
        self._cost_by_model_counter = Counter(
            "gateway_cost_by_model",
            "Total cost by model",
            labelnames=["model"],
            registry=registry,
        )

        # gateway_cost_by_user — counter
        self._cost_by_user_counter = Counter(
            "gateway_cost_by_user",
            "Total cost by user",
            labelnames=["user_id"],
            registry=registry,
        )

        # gateway_circuit_breaker_state — gauge
        self._circuit_breaker_gauge = Gauge(
            "gateway_circuit_breaker_state",
            "Circuit breaker state per provider",
            labelnames=["provider"],
            registry=registry,
        )

        # gateway_active_requests — gauge
        self._active_requests_gauge = Gauge(
            "gateway_active_requests",
            "Currently active requests",
            registry=registry,
        )

        # gateway_up — gauge
        self._up_gauge = Gauge(
            "gateway_up",
            "Whether the gateway is healthy",
            registry=registry,
        )
        self._up_gauge.set(1)

        logger.info("Prometheus 指标初始化完成")

    # ------------------------------------------------------------------
    # HTTP 请求追踪
    # ------------------------------------------------------------------

    def record_request(
        self,
        method: str,
        endpoint: str,
        status: str,
    ) -> None:
        """记录 HTTP 请求完成。

        Args:
            method: HTTP 方法，如 "POST"。
            endpoint: 请求端点，如 "/v1/chat/completions"。
            status: HTTP 状态码，如 "200"。
        """
        if not self.enabled:
            return

        if self._requests_counter:
            self._requests_counter.labels(
                method=method, endpoint=endpoint, status=status
            ).inc()

    def record_duration(
        self,
        endpoint: str,
        duration_seconds: float,
    ) -> None:
        """记录请求持续时间。

        Args:
            endpoint: 请求端点。
            duration_seconds: 持续时间（秒）。
        """
        if not self.enabled:
            return

        if self._duration_histogram:
            self._duration_histogram.labels(endpoint=endpoint).observe(
                duration_seconds
            )

    def inc_active(self) -> None:
        """增加活跃请求计数。"""
        if not self.enabled:
            return

        if self._active_requests_gauge:
            self._active_requests_gauge.inc()

    def dec_active(self) -> None:
        """减少活跃请求计数。"""
        if not self.enabled:
            return

        if self._active_requests_gauge:
            self._active_requests_gauge.dec()

    # ------------------------------------------------------------------
    # 缓存指标
    # ------------------------------------------------------------------

    def inc_cache_hits(self, tier: str = "L1") -> None:
        """增加缓存命中计数。

        Args:
            tier: 缓存层级 "L1" | "L2" | "L3"。
        """
        if not self.enabled:
            return

        if self._cache_hits_counter:
            self._cache_hits_counter.labels(tier=tier).inc()

    def inc_cache_misses(self) -> None:
        """增加缓存未命中计数。"""
        if not self.enabled:
            return

        if self._cache_misses_counter:
            self._cache_misses_counter.inc()

    # ------------------------------------------------------------------
    # Token 和成本指标
    # ------------------------------------------------------------------

    def record_tokens(self, tokens: int, token_type: str = "prompt") -> None:
        """记录 token 消耗。

        Args:
            tokens: token 数量。
            token_type: token 类型 "prompt" | "completion"。
        """
        if not self.enabled:
            return

        if self._tokens_counter and tokens > 0:
            self._tokens_counter.labels(type=token_type).inc(tokens)

    def record_tokens_saved(self, tokens: int) -> None:
        """记录缓存命中节省的 token 数。

        Args:
            tokens: 节省的 token 数量。
        """
        if not self.enabled:
            return

        if self._tokens_saved_counter and tokens > 0:
            self._tokens_saved_counter.inc(tokens)
            return

        if self._tokens_counter and tokens > 0:
            self._tokens_counter.labels(type=token_type).inc(tokens)

    def record_cost(self, cost_usd: float, model: str = "unknown", user_id: str = "") -> None:
        """记录请求成本。

        Args:
            cost_usd: 成本（美元）。
            model: 模型名称。
            user_id: 用户 ID。
        """
        if not self.enabled:
            return

        if self._cost_total_gauge:
            # 累加总成本（Gauge 没有 inc(amount)，使用 _value 直接累加）
            self._cost_total_gauge.inc(cost_usd)

        if self._cost_by_model_counter:
            self._cost_by_model_counter.labels(model=model).inc(cost_usd)

        if self._cost_by_user_counter and user_id:
            self._cost_by_user_counter.labels(user_id=user_id).inc(cost_usd)

    # ------------------------------------------------------------------
    # 熔断器状态指标
    # ------------------------------------------------------------------

    def set_circuit_breaker_state(self, provider: str, state: int) -> None:
        """设置提供商熔断器状态。

        Args:
            provider: 提供商名称，如 "openai"。
            state: 状态值 0=CLOSED, 1=OPEN, 2=HALF-OPEN。
        """
        if not self.enabled:
            return

        if self._circuit_breaker_gauge:
            self._circuit_breaker_gauge.labels(provider=provider).set(state)

    # ------------------------------------------------------------------
    # 健康状态
    # ------------------------------------------------------------------

    def set_up(self, healthy: bool = True) -> None:
        """设置服务健康状态。

        Args:
            healthy: 是否健康。
        """
        if not self.enabled:
            return

        if self._up_gauge:
            self._up_gauge.set(1 if healthy else 0)

    def get_uptime_seconds(self) -> int:
        """获取服务运行时间（秒）。

        Returns:
            从初始化到现在经过的秒数。
        """
        return int(time.time() - self._start_time)

    # ------------------------------------------------------------------
    # 指标导出
    # ------------------------------------------------------------------

    def collect_all(self) -> Dict[str, Any]:
        """收集所有指标的当前值（用于调试 / 内省）。

        Returns:
            指标名称 -> 值的映射。
        """
        if not self.enabled:
            return {}

        result: Dict[str, Any] = {}

        # 手动收集每个指标的采样值
        if self._active_requests_gauge:
            result["gateway_active_requests"] = self._active_requests_gauge._value.get()

        if self._up_gauge:
            result["gateway_up"] = self._up_gauge._value.get()

        result["uptime_seconds"] = self.get_uptime_seconds()

        return result

    # ------------------------------------------------------------------
    # 上下文管理器 — 自动记录请求
    # ------------------------------------------------------------------

    def track_request(
        self,
        endpoint: str,
        method: str = "POST",
    ) -> RequestTracker:
        """返回一个上下文管理器，自动记录 HTTP 请求的持续时间和计数器。

        Args:
            endpoint: 请求端点。
            method: HTTP 方法。

        Returns:
            RequestTracker 上下文管理器实例。
        """
        return RequestTracker(
            collector=self,
            endpoint=endpoint,
            method=method,
        )


class RequestTracker:
    """HTTP 请求追踪的上下文管理器。

    用法:
        with metrics.track_request("/v1/chat/completions"):
            # 业务逻辑
            pass
    """

    def __init__(
        self,
        collector: MetricsCollector,
        endpoint: str,
        method: str = "POST",
    ) -> None:
        self.collector = collector
        self.endpoint = endpoint
        self.method = method
        self.start_time: float = 0.0

    def __enter__(self) -> "RequestTracker":
        """进入时记录起始时间并增加活跃计数。"""
        self.start_time = time.time()
        self.collector.inc_active()
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[Exception],
        exc_tb: Any,
    ) -> None:
        """退出时记录持续时间并减少活跃计数。"""
        duration = time.time() - self.start_time

        # 确定状态码
        if exc_val is not None:
            status = "500"
        else:
            status = "200"

        self.collector.record_request(self.method, self.endpoint, status)
        self.collector.record_duration(self.endpoint, duration)
        self.collector.dec_active()


# ------------------------------------------------------------------
# 全局单例
# ------------------------------------------------------------------

_collector_instance: Optional[MetricsCollector] = None


def get_metrics_collector() -> MetricsCollector:
    """获取全局 MetricsCollector 单例。

    Returns:
        MetricsCollector 实例。
    """
    global _collector_instance

    if _collector_instance is None:
        # 从环境变量读取配置
        enabled = os.environ.get("AI_GATEWAY_PROMETHEUS_ENABLED", "true").lower() in (
            "true", "1", "yes"
        )
        _collector_instance = MetricsCollector(enabled=enabled)
        _collector_instance.initialize()

    return _collector_instance


def reset_metrics_collector() -> None:
    """重置全局指标收集器（主要用于测试）。"""
    global _collector_instance
    _collector_instance = None
