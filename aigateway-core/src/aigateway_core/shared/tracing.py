"""
Tracing — OpenTelemetry 追踪注入
==============================

为每个请求生成 trace_id，并注入 OTel span。
支持配置化的采样率（AI_GATEWAY_OTEL_SAMPLE_RATE）。

根据 TECH_SPEC.md 链路追踪规范:
- OpenTelemetry SDK 1.24+
- trace_id 贯穿全管线
- 采样率可配置，默认 0.1
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 全局单例
# ------------------------------------------------------------------

_tracing_instance: Optional[TracingManager] = None


class TracingManager:
    """OpenTelemetry 追踪管理器。

    负责:
    1. 初始化 OTel  tracer_provider 和 meter_provider
    2. 为每个请求创建 span
    3. 自动注入 trace_id / span_id 到日志和指标

    属性:
        enabled: 是否启用追踪。
        service_name: OTel 服务名。
        sample_rate: 采样率 (0.0-1.0)。
        _tracer: OTel Tracer 实例。
    """

    def __init__(
        self,
        enabled: bool = True,
        service_name: str = "ai-gateway",
        sample_rate: float = 0.1,
    ) -> None:
        """
        Args:
            enabled: 是否启用 OTel 追踪，默认 True。
            service_name: OTel 服务名，默认 "ai-gateway"。
            sample_rate: 采样率，默认 0.1（10%）。
        """
        self.enabled = enabled
        self.service_name = service_name
        self.sample_rate = max(0.0, min(1.0, sample_rate))

        self._tracer: Any = None
        self._initialized = False

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """初始化 OTel Tracer。

        仅在启用追踪时加载 OTel SDK。
        """
        if not self.enabled:
            logger.info("OTel 追踪已禁用，跳过初始化")
            return

        if self._initialized:
            return

        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import (
                BatchSpanProcessor,
                ConsoleSpanExporter,
            )
            from opentelemetry.trace import SpanKind, Status, StatusCode

            # 设置 Resource
            resource = Resource.create({
                "service.name": self.service_name,
                "service.version": "1.0.0",
            })

            # 配置 TracerProvider
            provider = TracerProvider(resource=resource)

            # 配置采样器
            from opentelemetry.trace.sampling import ParentBasedTraceIdRatio
            sampler = ParentBasedTraceIdRatio(self.sample_rate)
            provider.set_sampler(sampler)

            # 添加 console exporter（调试用，生产可替换为 Jaeger/Zipkin）
            processor = BatchSpanProcessor(ConsoleSpanExporter())
            provider.add_span_processor(processor)

            trace.set_tracer_provider(provider)

            self._tracer = trace.get_tracer(
                self.service_name,
                version="1.0.0",
                span_kind=SpanKind.SERVER,
            )

            self._initialized = True
            logger.info(
                "OTel 追踪已初始化: service=%s, sample_rate=%.2f",
                self.service_name,
                self.sample_rate,
            )

        except ImportError:
            logger.warning(
                "opentelemetry-sdk 未安装，追踪功能不可用。"
                "请执行: pip install opentelemetry-api opentelemetry-sdk"
            )
            self._tracer = None

        except Exception as exc:
            logger.error("OTel 追踪初始化失败: %s", exc)
            self._tracer = None

    def ensure_initialized(self) -> None:
        """确保追踪器已初始化（懒加载）。"""
        if not self._initialized:
            self.initialize()

    # ------------------------------------------------------------------
    # Span 创建
    # ------------------------------------------------------------------

    def create_request_span(
        self,
        request_id: str,
        trace_id: Optional[str] = None,
        operation: str = "gateway_request",
    ) -> Dict[str, Any]:
        """为请求创建 OTel span。

        创建后返回 span 上下文字典，供插件在执行期间设置属性和记录事件。

        Args:
            request_id: 请求 ID。
            trace_id: 已有的 trace_id，若不提供则生成新的。
            operation: span 操作名，默认 "gateway_request"。

        Returns:
            包含 span 和 context 的字典:
            {
                "span": OTel Span,
                "trace_id": str,
                "span_id": str,
                "started_at": float,
            }
            若追踪未启用则返回空字典。
        """
        self.ensure_initialized()

        if not self._tracer or not self.enabled:
            return {}

        import uuid

        tid = trace_id or uuid.uuid4().hex

        with self._tracer.start_as_current_span(
            name=f"{operation}.{request_id}",
            kind=SpanKind.SERVER,
        ) as span:
            # 注入属性
            span.set_attribute("request.id", request_id)
            span.set_attribute("trace.id", tid)
            span.set_attribute("service.name", self.service_name)

            span_id = span.context.span_id if span.context else 0
            span_hex = format(span_id, "016x")

            return {
                "span": span,
                "trace_id": tid,
                "span_id": span_hex,
                "started_at": time.time(),
            }

    def create_plugin_span(
        self,
        span_context: Dict[str, Any],
        plugin_name: str,
        request_id: str,
    ) -> Dict[str, Any]:
        """为单个插件创建子 span。

        Args:
            span_context: create_request_span 返回的上下文。
            plugin_name: 插件名称。
            request_id: 请求 ID。

        Returns:
            子 span 上下文字典。
        """
        if not span_context or not self.enabled:
            return {}

        self.ensure_initialized()

        tid = span_context.get("trace_id", "")
        span_attrs: Dict[str, Any] = {
            "plugin.name": plugin_name,
            "request.id": request_id,
            "trace.id": tid,
        }

        return {
            "plugin_name": plugin_name,
            "started_at": time.time(),
            "attributes": span_attrs,
        }

    # ------------------------------------------------------------------
    # Span 属性与事件
    # ------------------------------------------------------------------

    @staticmethod
    def set_span_attribute(
        otel_span: Any,
        key: str,
        value: Any,
    ) -> None:
        """为 OTel span 设置属性。

        Args:
            otel_span: OTel Span 对象。
            key: 属性键。
            value: 属性值。
        """
        if otel_span is None:
            return

        try:
            otel_span.set_attribute(key, value)
        except Exception as exc:
            logger.debug("设置 span 属性失败: %s", exc)

    @staticmethod
    def add_span_event(
        otel_span: Any,
        name: str,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> None:
        """为 OTel span 添加事件。

        Args:
            otel_span: OTel Span 对象。
            name: 事件名称。
            attributes: 事件属性字典。
        """
        if otel_span is None:
            return

        try:
            otel_span.add_event(name, attributes or {})
        except Exception as exc:
            logger.debug("添加 span 事件失败: %s", exc)

    @staticmethod
    def mark_span_error(
        otel_span: Any,
        error: Exception,
    ) -> None:
        """标记 span 为错误状态。

        Args:
            otel_span: OTel Span 对象。
            error: 异常对象。
        """
        if otel_span is None:
            return

        try:
            otel_span.set_status(Status(StatusCode.ERROR, str(error)))
            otel_span.record_exception(error)
        except Exception as exc:
            logger.debug("标记 span 错误失败: %s", exc)

    # ------------------------------------------------------------------
    # Trace ID 传播
    # ------------------------------------------------------------------

    @staticmethod
    def inject_trace_context(headers: Dict[str, str], trace_id: str, span_id: str) -> None:
        """将 trace context 注入 HTTP 请求头，用于跨服务传播。

        Args:
            headers: 请求头字典（原地修改）。
            trace_id: Trace ID。
            span_id: 当前 Span ID。
        """
        headers["traceparent"] = f"00-{trace_id}-{span_id}-01"
        headers["X-Trace-ID"] = trace_id
        headers["X-Span-ID"] = span_id

    @staticmethod
    def extract_trace_context(headers: Dict[str, str]) -> Dict[str, str]:
        """从 HTTP 请求头提取 trace context。

        Args:
            headers: 请求头字典。

        Returns:
            {"trace_id": str, "span_id": str}。
        """
        traceparent = headers.get("traceparent", "")
        trace_id = headers.get("X-Trace-ID", "")
        span_id = headers.get("X-Span-ID", "")

        # 解析 W3C traceparent 格式: 00-traceId-spanId-flags
        if traceparent:
            parts = traceparent.split("-")
            if len(parts) >= 3:
                trace_id = parts[1]
                span_id = parts[2]

        return {
            "trace_id": trace_id or "",
            "span_id": span_id or "",
        }

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def get_trace_info(self) -> Dict[str, Any]:
        """获取当前追踪配置信息。

        Returns:
            追踪配置摘要。
        """
        return {
            "enabled": self.enabled,
            "service_name": self.service_name,
            "sample_rate": self.sample_rate,
            "initialized": self._initialized,
        }


def get_tracing_manager() -> TracingManager:
    """获取全局 TracingManager 单例。

    从环境变量读取配置。

    Returns:
        TracingManager 实例。
    """
    global _tracing_instance

    if _tracing_instance is None:
        # 从环境变量读取配置
        enabled = os.environ.get("AI_GATEWAY_OPENTELEMETRY_ENABLED", "true").lower() in (
            "true", "1", "yes"
        )
        service_name = os.environ.get("AI_GATEWAY_OTEL_SERVICE_NAME", "ai-gateway")
        sample_rate_str = os.environ.get("AI_GATEWAY_OTEL_SAMPLE_RATE", "0.1")

        try:
            sample_rate = float(sample_rate_str)
        except ValueError:
            sample_rate = 0.1

        _tracing_instance = TracingManager(
            enabled=enabled,
            service_name=service_name,
            sample_rate=sample_rate,
        )

    return _tracing_instance
