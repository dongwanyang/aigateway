"""
CostTrackerPlugin — 成本追踪插件封装
======================================

将 GenerationCostTracker 封装为 PipelineEngine 插件，注册到 PluginRegistry。
在 execute() 中通过 emit_plugin_event 发 TraceEvent,汇总各优化策略带来的成本节省，
记录到请求元数据并上报 Prometheus 指标。

需求: 7.1, 1.8
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from aigateway_core.context import PipelineContext
from aigateway_core.generation_optimization.config import GenerationOptimizationConfig
from aigateway_core.generation_optimization.metrics import GenerationCostTracker
from aigateway_core.generation_optimization.models import CostSavingRecord

logger = logging.getLogger(__name__)

# 命名空间常量
NS_GENERATION_OPTIMIZATION = "generation_optimization"


class CostTrackerPlugin:
    """成本追踪插件 — 将 GenerationCostTracker 封装为 PipelineEngine 插件.

    通过 PluginRegistry 注册后由 PipelineEngine 自动调度执行。
    依赖 gen_model_router 插件先行执行（确保路由决策数据已写入上下文）。

    行为:
    - 禁用时: 透传请求，不做任何计算
    - 启用时:
      1. 创建子 span 用于追踪
      2. 从 ctx.extra["generation_optimization"] 中收集各策略的数据:
         - AI Director: cost_usd (director_cost)
         - Model Router: estimated_cost (actual_price), premium_price 从配置的最高能力模型推算
         - Token Compressor: original_token_count, compressed_token_count
      3. 调用 tracker.record_model_routing_saving()
      4. 调用 tracker.record_token_compression_saving()
      5. 调用 tracker.record_prompt_optimization_saving()
      6. 调用 tracker.record_total_saving()
      7. 将 CostSavingRecord 写入 ctx.extra["generation_optimization"]["cost_tracker"]
      8. 记录 span 属性

    Attributes:
        name: 插件名称 "cost_tracker"
        enabled: 是否启用
        depends_on: 依赖的插件列表 ["gen_model_router"]
    """

    name: str = "cost_tracker"
    enabled: bool = True
    depends_on: List[str] = ["gen_model_router"]

    def __init__(
        self,
        tracker: GenerationCostTracker,
        config: GenerationOptimizationConfig,
    ) -> None:
        """初始化 CostTrackerPlugin.

        Args:
            tracker: GenerationCostTracker 实例，负责成本节省计算与记录
            config: 生成优化层主配置实例
        """
        self._tracker = tracker
        self._config = config

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        """执行成本追踪计算.

        流程:
        1. 检查是否禁用 → 禁用时直接透传
        2. 创建子 span 用于追踪 (需求 1.8)
        3. 从上下文中收集各策略数据
        4. 计算各策略成本节省
        5. 汇总并生成 CostSavingRecord
        6. 写入 ctx.extra["generation_optimization"]["cost_tracker"]
        7. 记录 span 属性
        8. 任何计算失败记录零节省并继续 (需求 7.5)

        Args:
            ctx: 管线上下文

        Returns:
            修改后的管线上下文
        """
        # 1. 检查是否禁用
        if not self._config.cost_tracking.enabled:
            logger.debug(
                "generation_optimization.cost_tracker.disabled",
                extra={
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                },
            )
            return ctx

        start_time = time.monotonic()

        try:
            # 3. 收集各策略数据
            gen_opt = ctx.extra.get(NS_GENERATION_OPTIMIZATION, {})
            request_id = ctx.request_id
            # 获取 api_key_id（用于 group 标签注入 Prometheus 指标, 需求 9.2, 9.4）
            api_key_id = ctx.user_id or ""

            # --- 模型路由节省 ---
            routing_saving = self._calculate_routing_saving(gen_opt, request_id, api_key_id)

            # --- Token 压缩节省 ---
            compression_saving = self._calculate_compression_saving(gen_opt, request_id, api_key_id)

            # --- Prompt 优化节省 ---
            prompt_saving = self._calculate_prompt_saving(gen_opt, request_id, api_key_id)

            # 6. 汇总并生成 CostSavingRecord
            record: CostSavingRecord = self._tracker.record_total_saving(
                request_id=request_id,
                routing=routing_saving,
                compression=compression_saving,
                prompt=prompt_saving,
            )

            # 7. 写入结果到 ctx.extra
            duration_ms = (time.monotonic() - start_time) * 1000.0
            gen_opt_ns = ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})
            gen_opt_ns["cost_tracker"] = {
                "request_id": record.request_id,
                "model_routing_saving_usd": record.model_routing_saving_usd,
                "token_compression_saving_usd": record.token_compression_saving_usd,
                "prompt_optimization_saving_usd": record.prompt_optimization_saving_usd,
                "total_saving_usd": record.total_saving_usd,
                "timestamp": record.timestamp,
                "duration_ms": duration_ms,
            }

            logger.info(
                "generation_optimization.cost_tracker.completed",
                extra={
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                    "model_routing_saving_usd": record.model_routing_saving_usd,
                    "token_compression_saving_usd": record.token_compression_saving_usd,
                    "prompt_optimization_saving_usd": record.prompt_optimization_saving_usd,
                    "total_saving_usd": record.total_saving_usd,
                    "duration_ms": round(duration_ms, 2),
                },
            )

            # 发 TraceEvent(成功)
            from aigateway_core.generation_optimization.plugins import emit_plugin_event

            emit_plugin_event(ctx, self.name, duration_ms, "ok")

        except Exception as exc:
            # 任何错误不阻断管线，记录零节省 (需求 7.5)
            duration_ms = (time.monotonic() - start_time) * 1000.0

            # 发 TraceEvent(失败)
            from aigateway_core.generation_optimization.plugins import emit_plugin_event

            emit_plugin_event(ctx, self.name, duration_ms, "error")

            logger.warning(
                "generation_optimization.cost_tracker.error",
                extra={
                    "reason": str(exc),
                    "fallback_action": "record_zero_savings",
                    "request_id": ctx.request_id,
                    "trace_id": ctx.trace_id,
                    "duration_ms": round(duration_ms, 2),
                },
            )
            # 写入零节省记录
            gen_opt_ns = ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})
            gen_opt_ns["cost_tracker"] = {
                "request_id": ctx.request_id,
                "model_routing_saving_usd": 0.0,
                "token_compression_saving_usd": 0.0,
                "prompt_optimization_saving_usd": 0.0,
                "total_saving_usd": 0.0,
                "timestamp": time.time(),
                "duration_ms": duration_ms,
                "error": str(exc),
            }

        return ctx

    # ------------------------------------------------------------------
    # 各策略节省计算辅助方法
    # ------------------------------------------------------------------

    def _calculate_routing_saving(
        self, gen_opt: Dict[str, Any], request_id: str, api_key_id: str = ""
    ) -> float:
        """计算模型路由成本节省.

        从 model_router 数据中获取 estimated_cost（实际模型价格），
        从配置中推算 premium_price（最高能力模型的价格）。

        公式: max(0, premium_price - actual_price)

        Args:
            gen_opt: generation_optimization 命名空间数据
            request_id: 请求标识
            api_key_id: API Key 标识符（用于 group 标签注入 Prometheus 指标）

        Returns:
            路由节省金额 (USD)
        """
        try:
            router_data = gen_opt.get("model_router", {})
            actual_price = router_data.get("estimated_cost", 0.0)

            # 获取 premium_price: 配置中最高能力模型的价格
            premium_price = self._get_premium_price(gen_opt)

            if premium_price <= 0.0 or actual_price < 0.0:
                return 0.0

            return self._tracker.record_model_routing_saving(
                premium_price=premium_price,
                actual_price=actual_price,
                request_id=request_id,
                api_key_id=api_key_id,
            )
        except Exception as exc:
            logger.warning(
                "generation_optimization.cost_tracker.routing_saving_failed",
                extra={
                    "reason": str(exc),
                    "request_id": request_id,
                },
            )
            return 0.0

    def _calculate_compression_saving(
        self, gen_opt: Dict[str, Any], request_id: str, api_key_id: str = ""
    ) -> float:
        """计算 Token 压缩成本节省.

        从 token_compressor 数据中获取 original 和 compressed token 计数，
        从 model_router 获取当前模型的 per-token 价格估算。

        公式: max(0, (original - compressed) * per_token_price)

        Args:
            gen_opt: generation_optimization 命名空间数据
            request_id: 请求标识
            api_key_id: API Key 标识符（用于 group 标签注入 Prometheus 指标）

        Returns:
            压缩节省金额 (USD)
        """
        try:
            compressor_data = gen_opt.get("token_compressor", {})
            original_tokens = compressor_data.get("total_original_tokens", 0)
            compressed_tokens = compressor_data.get("total_compressed_tokens", 0)

            if original_tokens <= 0 or compressed_tokens < 0:
                return 0.0

            # 估算每 token 价格:
            # 从 model_router 的 estimated_cost 和请求预估 token 数推算
            # 或使用简化的默认值（基于常见 MLLM 定价）
            per_token_price = self._estimate_per_token_price(gen_opt)

            return self._tracker.record_token_compression_saving(
                original_tokens=original_tokens,
                compressed_tokens=compressed_tokens,
                per_token_price=per_token_price,
                request_id=request_id,
                api_key_id=api_key_id,
            )
        except Exception as exc:
            logger.warning(
                "generation_optimization.cost_tracker.compression_saving_failed",
                extra={
                    "reason": str(exc),
                    "request_id": request_id,
                },
            )
            return 0.0

    def _calculate_prompt_saving(
        self, gen_opt: Dict[str, Any], request_id: str, api_key_id: str = ""
    ) -> float:
        """计算 Prompt 优化净节省.

        从 ai_director 数据中获取 director_cost，从 model_router 获取
        生成成本，使用配置的 assumed_retry_rate 计算减少重试带来的节省。

        公式: max(0, retry_rate * generation_cost - director_cost)

        Args:
            gen_opt: generation_optimization 命名空间数据
            request_id: 请求标识
            api_key_id: API Key 标识符（用于 group 标签注入 Prometheus 指标）

        Returns:
            Prompt 优化净节省金额 (USD)
        """
        try:
            director_data = gen_opt.get("ai_director", {})
            director_cost = director_data.get("cost_usd", 0.0)
            model_used = director_data.get("model_used")

            # 获取生成成本（从 model_router 的 estimated_cost）
            router_data = gen_opt.get("model_router", {})
            generation_cost = router_data.get("estimated_cost", 0.0)

            # 使用配置的假定重试率
            retry_rate = self._config.cost_tracking.assumed_retry_rate

            if generation_cost <= 0.0:
                return 0.0

            return self._tracker.record_prompt_optimization_saving(
                retry_rate=retry_rate,
                generation_cost=generation_cost,
                director_cost=director_cost,
                request_id=request_id,
                api_key_id=api_key_id,
                model_used=model_used,
            )
        except Exception as exc:
            logger.warning(
                "generation_optimization.cost_tracker.prompt_saving_failed",
                extra={
                    "reason": str(exc),
                    "request_id": request_id,
                },
            )
            return 0.0

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _get_premium_price(self, gen_opt: Dict[str, Any]) -> float:
        """获取 premium 模型价格（最高能力模型的价格）.

        从 gen_opt 中的 model_router 数据获取 premium_price，
        如果未提供则从配置的 model_capabilities 中找到最高能力模型
        并从上下文获取其价格。

        Args:
            gen_opt: generation_optimization 命名空间数据

        Returns:
            Premium 模型每次请求价格 (USD)
        """
        # 优先从 model_router 数据中获取（如果路由器已记录）
        router_data = gen_opt.get("model_router", {})
        premium_price = router_data.get("premium_price")
        if premium_price is not None and premium_price > 0:
            return float(premium_price)

        # 从 estimated_cost 推断: 如果 reason 是 "complexity"，
        # 意味着选了一个可能更便宜的模型。需要从配置获取最高能力模型的价格。
        # 如果没有更好的数据源，返回 estimated_cost 作为保守估计（节省为 0）
        estimated_cost = router_data.get("estimated_cost", 0.0)

        # 如果上下文中明确提供了 premium_model_price
        premium_from_ctx = gen_opt.get("premium_model_price")
        if premium_from_ctx is not None and premium_from_ctx > 0:
            return float(premium_from_ctx)

        # 兜底: 返回 estimated_cost（此时路由节省为 0）
        return estimated_cost

    def _estimate_per_token_price(self, gen_opt: Dict[str, Any]) -> float:
        """估算每个 token 的价格.

        基于模型路由的 estimated_cost 和典型请求 token 数量推算，
        或使用保守的默认值。

        常见多模态模型定价参考:
        - GPT-4 Vision: ~$0.00001/token (input image tokens)
        - Claude 3.5: ~$0.000003/token
        - 保守默认: $0.000005/token

        Args:
            gen_opt: generation_optimization 命名空间数据

        Returns:
            每 token 价格估算 (USD)
        """
        # 优先从上下文中获取明确的 per_token_price
        per_token_price = gen_opt.get("per_token_price")
        if per_token_price is not None and per_token_price > 0:
            return float(per_token_price)

        # 从 model_router 数据推算
        router_data = gen_opt.get("model_router", {})
        per_token = router_data.get("per_token_price")
        if per_token is not None and per_token > 0:
            return float(per_token)

        # 保守默认值: $0.000005/token (5 微美元)
        return 0.000005
