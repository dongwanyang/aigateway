"""
指标层 — 生成优化层成本追踪与 Prometheus 指标
=============================================

定义生成优化层特有的 Prometheus 指标，包括：
- gen_opt_savings_usd_total: 各策略成本节省（counter, labels: strategy, api_key_group）
- gen_opt_invocations_total: 各策略调用次数（counter, labels: strategy, api_key_group）
- gen_opt_net_savings_usd: 累计净节省（gauge）
- gen_opt_prompt_optimizations_total: Prompt 优化成功次数（counter）
- gen_opt_director_cost_usd_total: AI Director 调用成本（counter, labels: model）

需求: 6.1, 6.2, 6.7, 7.1, 7.2, 7.3, 7.4, 7.5, 9.1, 9.2, 9.3, 9.4
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from aigateway_core.generation_optimization.config import CostTrackingConfig
from aigateway_core.generation_optimization.models import CostSavingRecord

logger = logging.getLogger(__name__)

# Prometheus 指标名称常量
METRIC_SAVINGS_USD_TOTAL = "gen_opt_savings_usd_total"
METRIC_INVOCATIONS_TOTAL = "gen_opt_invocations_total"
METRIC_NET_SAVINGS_USD = "gen_opt_net_savings_usd"
METRIC_PROMPT_OPTIMIZATIONS_TOTAL = "gen_opt_prompt_optimizations_total"
METRIC_DIRECTOR_COST_USD_TOTAL = "gen_opt_director_cost_usd_total"

# 策略标签值
STRATEGY_MODEL_ROUTING = "model_routing"
STRATEGY_TOKEN_COMPRESSION = "token_compression"
STRATEGY_PROMPT_OPTIMIZATION = "prompt_optimization"

# 默认 API Key group 标签
DEFAULT_API_KEY_GROUP = "default"


class PrometheusMetricsRegistry:
    """Prometheus 指标注册表 — 管理生成优化层的 Prometheus 指标.

    尝试导入 prometheus_client，如果未安装则优雅降级（所有操作变为 no-op）。
    注册 Counter 和 Gauge 指标供 GenerationCostTracker 使用。

    需求: 7.2, 7.3, 9.1, 9.2, 9.3, 9.4
    """

    def __init__(self, registry: Any = None) -> None:
        """初始化 Prometheus 指标注册表.

        尝试导入 prometheus_client 并注册所有指标。
        如果 prometheus_client 未安装，则设置 available=False，所有操作变为 no-op。

        Args:
            registry: 可选的 prometheus_client CollectorRegistry 实例。
                      传入 None 时使用默认全局 registry。
        """
        self._available = False
        self._savings_counter: Any = None
        self._invocations_counter: Any = None
        self._net_savings_gauge: Any = None
        self._prompt_optimizations_counter: Any = None
        self._director_cost_counter: Any = None
        self._collector_registry: Any = None

        try:
            from prometheus_client import CollectorRegistry, Counter, Gauge

            # Use a dedicated registry per instance to avoid duplication errors
            # when creating multiple instances (e.g., in tests).
            if registry is None:
                registry = CollectorRegistry()
            self._collector_registry = registry

            self._savings_counter = Counter(
                METRIC_SAVINGS_USD_TOTAL,
                "Total cost savings in USD by strategy and API key group",
                labelnames=["strategy", "api_key_group"],
                registry=registry,
            )

            self._invocations_counter = Counter(
                METRIC_INVOCATIONS_TOTAL,
                "Total optimization invocations by strategy and API key group",
                labelnames=["strategy", "api_key_group"],
                registry=registry,
            )

            self._net_savings_gauge = Gauge(
                METRIC_NET_SAVINGS_USD,
                "Cumulative net savings in USD",
                registry=registry,
            )

            self._prompt_optimizations_counter = Counter(
                METRIC_PROMPT_OPTIMIZATIONS_TOTAL,
                "Total successful prompt optimizations",
                registry=registry,
            )

            self._director_cost_counter = Counter(
                METRIC_DIRECTOR_COST_USD_TOTAL,
                "Total AI Director model invocation cost in USD by model",
                labelnames=["model"],
                registry=registry,
            )

            self._available = True
            logger.info("生成优化层 Prometheus 指标注册完成")

        except ImportError:
            logger.warning(
                "prometheus_client 未安装，生成优化层指标功能不可用。"
                "请执行: pip install prometheus-client"
            )
        except Exception as exc:
            logger.error(
                "生成优化层 Prometheus 指标注册失败: %s", exc
            )

    @property
    def available(self) -> bool:
        """指标是否可用（prometheus_client 已安装且指标注册成功）."""
        return self._available

    def inc_savings(self, strategy: str, api_key_group: str, amount: float) -> None:
        """递增成本节省计数器.

        Args:
            strategy: 策略标签值 (model_routing/token_compression/prompt_optimization)
            api_key_group: API Key 分组标签
            amount: 节省金额 (USD)
        """
        if not self._available or amount <= 0:
            return
        try:
            self._savings_counter.labels(
                strategy=strategy, api_key_group=api_key_group
            ).inc(amount)
        except Exception as exc:
            logger.debug("Prometheus inc_savings 失败: %s", exc)

    def inc_invocations(self, strategy: str, api_key_group: str) -> None:
        """递增策略调用次数计数器.

        Args:
            strategy: 策略标签值
            api_key_group: API Key 分组标签
        """
        if not self._available:
            return
        try:
            self._invocations_counter.labels(
                strategy=strategy, api_key_group=api_key_group
            ).inc()
        except Exception as exc:
            logger.debug("Prometheus inc_invocations 失败: %s", exc)

    def set_net_savings(self, amount: float) -> None:
        """设置累计净节省 gauge 值.

        Args:
            amount: 累计净节省金额 (USD)
        """
        if not self._available:
            return
        try:
            self._net_savings_gauge.set(amount)
        except Exception as exc:
            logger.debug("Prometheus set_net_savings 失败: %s", exc)

    def inc_net_savings(self, amount: float) -> None:
        """递增累计净节省 gauge 值.

        Args:
            amount: 新增净节省金额 (USD)
        """
        if not self._available or amount <= 0:
            return
        try:
            self._net_savings_gauge.inc(amount)
        except Exception as exc:
            logger.debug("Prometheus inc_net_savings 失败: %s", exc)

    def inc_prompt_optimizations(self) -> None:
        """递增 Prompt 优化成功次数计数器."""
        if not self._available:
            return
        try:
            self._prompt_optimizations_counter.inc()
        except Exception as exc:
            logger.debug("Prometheus inc_prompt_optimizations 失败: %s", exc)

    def inc_director_cost(self, model: str, amount: float) -> None:
        """递增 AI Director 调用成本计数器.

        Args:
            model: 使用的模型名称
            amount: 调用成本 (USD)
        """
        if not self._available or amount <= 0:
            return
        try:
            self._director_cost_counter.labels(model=model).inc(amount)
        except Exception as exc:
            logger.debug("Prometheus inc_director_cost 失败: %s", exc)


# 全局单例（惰性初始化）
_prometheus_registry: Optional[PrometheusMetricsRegistry] = None


def get_prometheus_registry() -> PrometheusMetricsRegistry:
    """获取全局 PrometheusMetricsRegistry 单例.

    Returns:
        PrometheusMetricsRegistry 实例
    """
    global _prometheus_registry
    if _prometheus_registry is None:
        _prometheus_registry = PrometheusMetricsRegistry()
    return _prometheus_registry


def reset_prometheus_registry() -> None:
    """重置全局 Prometheus 注册表（主要用于测试）."""
    global _prometheus_registry
    _prometheus_registry = None


def _get_api_key_group(api_key_id: str, api_key_groups: Optional[Dict[str, str]] = None) -> str:
    """查找 API Key 的分组标签.

    根据 api_key_id 查找其所属的 group。如果未配置 group 或
    api_key_id 不在映射中，返回 "default"。

    Args:
        api_key_id: API Key 标识符
        api_key_groups: API Key ID 到 group 的映射表（可选）

    Returns:
        API Key 的分组标签，未分组时返回 "default"
    """
    if not api_key_id:
        return DEFAULT_API_KEY_GROUP
    if not api_key_groups:
        return DEFAULT_API_KEY_GROUP
    return api_key_groups.get(api_key_id, DEFAULT_API_KEY_GROUP)


class GenerationCostTracker:
    """成本追踪器 — 记录各策略带来的成本节省并上报 Prometheus.

    计算并记录模型路由、Token 压缩和 Prompt 优化三大策略的成本节省，
    精度为 6 位小数 (USD)。任何计算失败都会记录零节省并继续处理。

    集成 Prometheus 指标上报：
    - 每次记录节省时自动递增对应的 Prometheus counter
    - 支持 API Key group 标签（未分组的 API Key 使用 "default"）
    - 所有 Prometheus 操作包装在 try/except 中，指标失败不影响核心逻辑

    需求: 7.1, 7.2, 7.3, 7.4, 7.5, 9.1, 9.2, 9.3, 9.4
    """

    def __init__(
        self,
        config: CostTrackingConfig,
        prometheus_registry: Optional[PrometheusMetricsRegistry] = None,
        api_key_groups: Optional[Dict[str, str]] = None,
    ) -> None:
        """初始化成本追踪器.

        Args:
            config: 成本追踪配置，包含精度和假定重试率等参数
            prometheus_registry: Prometheus 指标注册表（可选，默认使用全局单例）
            api_key_groups: API Key ID 到 group 的映射表（可选）
        """
        self._config = config
        self._precision = config.precision_decimal_places
        self._prometheus = prometheus_registry or get_prometheus_registry()
        self._api_key_groups = api_key_groups or {}

    def record_model_routing_saving(
        self,
        premium_price: float,
        actual_price: float,
        request_id: str,
        api_key_id: str = "",
    ) -> float:
        """记录模型路由节省.

        计算公式: max(0, premium_price - actual_price)
        当选中的模型价格低于高端模型价格时产生节省。

        Args:
            premium_price: 高端模型的单次请求价格 (USD)
            actual_price: 实际选中模型的单次请求价格 (USD)
            request_id: 关联的请求标识
            api_key_id: API Key 标识符（用于分组标签）

        Returns:
            计算出的节省金额 (USD)，精度为 6 位小数。
            计算失败时返回 0.0。
        """
        try:
            saving = max(0.0, premium_price - actual_price)
            saving = round(saving, self._precision)
            logger.info(
                "generation_optimization.cost_saving.model_routing",
                extra={
                    "strategy": STRATEGY_MODEL_ROUTING,
                    "saving_usd": saving,
                    "premium_price": premium_price,
                    "actual_price": actual_price,
                    "request_id": request_id,
                },
            )
            # Prometheus 上报
            group = _get_api_key_group(api_key_id, self._api_key_groups)
            self._prometheus.inc_invocations(STRATEGY_MODEL_ROUTING, group)
            if saving > 0:
                self._prometheus.inc_savings(STRATEGY_MODEL_ROUTING, group, saving)
            return saving
        except Exception as exc:
            logger.warning(
                "generation_optimization.cost_saving.calculation_failed",
                extra={
                    "strategy": STRATEGY_MODEL_ROUTING,
                    "request_id": request_id,
                    "error": str(exc),
                },
            )
            return 0.0

    def record_token_compression_saving(
        self,
        original_tokens: int,
        compressed_tokens: int,
        per_token_price: float,
        request_id: str,
        api_key_id: str = "",
    ) -> float:
        """记录 Token 压缩节省.

        计算公式: max(0, (original_tokens - compressed_tokens) * per_token_price)

        Args:
            original_tokens: 原始 Token 数（= file_size_bytes / 4）
            compressed_tokens: 压缩后 Token 数（= Feature Vector 维度数）
            per_token_price: 每 Token 的价格 (USD)
            request_id: 关联的请求标识
            api_key_id: API Key 标识符（用于分组标签）

        Returns:
            计算出的节省金额 (USD)，精度为 6 位小数。
            计算失败时返回 0.0。
        """
        try:
            token_diff = original_tokens - compressed_tokens
            saving = max(0.0, token_diff * per_token_price)
            saving = round(saving, self._precision)
            logger.info(
                "generation_optimization.cost_saving.token_compression",
                extra={
                    "strategy": STRATEGY_TOKEN_COMPRESSION,
                    "saving_usd": saving,
                    "original_tokens": original_tokens,
                    "compressed_tokens": compressed_tokens,
                    "per_token_price": per_token_price,
                    "request_id": request_id,
                },
            )
            # Prometheus 上报
            group = _get_api_key_group(api_key_id, self._api_key_groups)
            self._prometheus.inc_invocations(STRATEGY_TOKEN_COMPRESSION, group)
            if saving > 0:
                self._prometheus.inc_savings(STRATEGY_TOKEN_COMPRESSION, group, saving)
            return saving
        except Exception as exc:
            logger.warning(
                "generation_optimization.cost_saving.calculation_failed",
                extra={
                    "strategy": STRATEGY_TOKEN_COMPRESSION,
                    "request_id": request_id,
                    "error": str(exc),
                },
            )
            return 0.0

    def record_prompt_optimization_saving(
        self,
        retry_rate: float,
        generation_cost: float,
        director_cost: float,
        request_id: str,
        api_key_id: str = "",
        model_used: Optional[str] = None,
    ) -> float:
        """记录 Prompt 优化净节省.

        计算公式: max(0, retry_rate * generation_cost - director_cost)
        优化 Prompt 减少重试带来的节省，减去 AI Director 自身的调用成本。

        Args:
            retry_rate: 假定重试率（默认 0.3，表示无优化时 30% 的请求需要重试）
            generation_cost: 单次生成的成本 (USD)
            director_cost: AI Director 模型调用成本 (USD)
            request_id: 关联的请求标识
            api_key_id: API Key 标识符（用于分组标签）
            model_used: AI Director 使用的模型名称（用于 Prometheus 标签）

        Returns:
            计算出的净节省金额 (USD)，精度为 6 位小数。
            计算失败时返回 0.0。
        """
        try:
            retry_saving = retry_rate * generation_cost
            saving = max(0.0, retry_saving - director_cost)
            saving = round(saving, self._precision)
            logger.info(
                "generation_optimization.cost_saving.prompt_optimization",
                extra={
                    "strategy": STRATEGY_PROMPT_OPTIMIZATION,
                    "saving_usd": saving,
                    "retry_rate": retry_rate,
                    "generation_cost": generation_cost,
                    "director_cost": director_cost,
                    "request_id": request_id,
                },
            )
            # Prometheus 上报
            group = _get_api_key_group(api_key_id, self._api_key_groups)
            self._prometheus.inc_invocations(STRATEGY_PROMPT_OPTIMIZATION, group)
            if saving > 0:
                self._prometheus.inc_savings(STRATEGY_PROMPT_OPTIMIZATION, group, saving)
            # 递增 Prompt 优化成功次数
            self._prometheus.inc_prompt_optimizations()
            # 记录 AI Director 调用成本
            if director_cost > 0 and model_used:
                self._prometheus.inc_director_cost(model_used, director_cost)
            return saving
        except Exception as exc:
            logger.warning(
                "generation_optimization.cost_saving.calculation_failed",
                extra={
                    "strategy": STRATEGY_PROMPT_OPTIMIZATION,
                    "request_id": request_id,
                    "error": str(exc),
                },
            )
            return 0.0

    def record_total_saving(
        self,
        request_id: str,
        routing: float,
        compression: float,
        prompt: float,
    ) -> CostSavingRecord:
        """汇总并记录单次请求的总成本节省.

        Args:
            request_id: 关联的请求标识
            routing: 模型路由节省 (USD)
            compression: Token 压缩节省 (USD)
            prompt: Prompt 优化净节省 (USD)

        Returns:
            CostSavingRecord 包含各策略节省明细和总节省。
            计算失败时各项为 0.0。
        """
        try:
            total = round(routing + compression + prompt, self._precision)
            record = CostSavingRecord(
                request_id=request_id,
                model_routing_saving_usd=routing,
                token_compression_saving_usd=compression,
                prompt_optimization_saving_usd=prompt,
                total_saving_usd=total,
                timestamp=time.time(),
            )
            logger.info(
                "generation_optimization.cost_saving.total",
                extra={
                    "request_id": request_id,
                    "model_routing_saving_usd": routing,
                    "token_compression_saving_usd": compression,
                    "prompt_optimization_saving_usd": prompt,
                    "total_saving_usd": total,
                },
            )
            # 更新 Prometheus 累计净节省 gauge
            if total > 0:
                self._prometheus.inc_net_savings(total)
            return record
        except Exception as exc:
            logger.warning(
                "generation_optimization.cost_saving.total_calculation_failed",
                extra={
                    "request_id": request_id,
                    "error": str(exc),
                },
            )
            return CostSavingRecord(
                request_id=request_id,
                timestamp=time.time(),
            )

    def report_to_prometheus(
        self,
        saving_record: CostSavingRecord,
        api_key_group: str = DEFAULT_API_KEY_GROUP,
        model_used: Optional[str] = None,
    ) -> None:
        """将成本节省记录上报到 Prometheus.

        该方法允许外部代码手动上报一条 CostSavingRecord，当各策略的
        record_* 方法已经内联上报时，通常无需单独调用。

        Args:
            saving_record: 成本节省记录
            api_key_group: API Key 分组标签（默认 "default"）
            model_used: AI Director 使用的模型名称（可选）
        """
        try:
            group = api_key_group or DEFAULT_API_KEY_GROUP

            # 上报各策略节省
            if saving_record.model_routing_saving_usd > 0:
                self._prometheus.inc_savings(
                    STRATEGY_MODEL_ROUTING, group,
                    saving_record.model_routing_saving_usd,
                )

            if saving_record.token_compression_saving_usd > 0:
                self._prometheus.inc_savings(
                    STRATEGY_TOKEN_COMPRESSION, group,
                    saving_record.token_compression_saving_usd,
                )

            if saving_record.prompt_optimization_saving_usd > 0:
                self._prometheus.inc_savings(
                    STRATEGY_PROMPT_OPTIMIZATION, group,
                    saving_record.prompt_optimization_saving_usd,
                )

            # 更新净节省 gauge
            if saving_record.total_saving_usd > 0:
                self._prometheus.inc_net_savings(saving_record.total_saving_usd)

            # 如果提供了 model_used，上报 Director 成本（此处无成本信息，仅做示例入口）
            # Director 成本在 record_prompt_optimization_saving 中已上报

        except Exception as exc:
            logger.debug(
                "generation_optimization.prometheus.report_failed",
                extra={
                    "request_id": saving_record.request_id,
                    "error": str(exc),
                },
            )
