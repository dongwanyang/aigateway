"""
PipelineContext — 共享状态
=========================

在异步插件管线中，PipelineContext 是所有插件共享的请求/响应状态对象。
它携带请求体、中间处理结果、追踪信息和插件间传递的命名空间数据。

根据 DB_SCHEMA.md In-Memory 数据结构 — §2 插件管线上下文 (PipelineContext) 定义。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 命名空间常量（extra 子字典的键），便于插件引用
# ------------------------------------------------------------------

NS_PROMPT_COMPRESS = "prompt_compress"
NS_PROMPT_CACHE = "prompt_cache"
NS_SEMANTIC_CACHE = "semantic_cache"
NS_PII_DETECTOR = "pii_detector"
NS_MODEL_ROUTER = "model_router"


@dataclass
class PipelineContext:
    """管线共享状态容器。

    每个进入网关的请求都会创建一个新的 PipelineContext 实例，
    插件按序在其中读写数据。

    属性:
        request: 原始 OpenAI 格式请求体。
        response: 缓存命中时设置的完整响应内容（JSON 字符串）。
        should_stop: 短路标记，True 时跳过后续插件。
        should_stream: 是否流式响应。
        trace_id: OpenTelemetry 追踪 ID（UUID4）。
        request_id: 唯一请求 ID（UUID4）。
        user_id: 从 API Key 解析出的用户 ID。
        extra: 插件间传递的命名空间数据字典。
    """

    # 必填字段
    request: Dict[str, Any]

    # 可选字段，带默认值
    response: Optional[str] = None
    should_stop: bool = False
    should_stream: bool = False
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    user_id: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # extra 命名空间的便捷访问器
    # ------------------------------------------------------------------

    # -- prompt_compress --

    @property
    def prompt_compress(self) -> Dict[str, Any]:
        """获取 prompt_compress 命名空间，不存在则自动创建。"""
        if NS_PROMPT_COMPRESS not in self.extra:
            self.extra[NS_PROMPT_COMPRESS] = {}
        return self.extra[NS_PROMPT_COMPRESS]  # type: ignore[no-any-return]

    @prompt_compress.setter
    def prompt_compress(self, value: Dict[str, Any]) -> None:
        self.extra[NS_PROMPT_COMPRESS] = value

    @property
    def original_length(self) -> int:
        """原始 prompt 长度。"""
        return self.prompt_compress.get("original_length", 0)

    @original_length.setter
    def original_length(self, value: int) -> None:
        self.prompt_compress["original_length"] = value

    @property
    def compressed_prompt(self) -> str:
        """压缩后的 prompt 文本。"""
        return self.prompt_compress.get("compressed_prompt", "")

    @compressed_prompt.setter
    def compressed_prompt(self, value: str) -> None:
        self.prompt_compress["compressed_prompt"] = value

    @property
    def compression_ratio(self) -> float:
        """压缩比例。"""
        return self.prompt_compress.get("compression_ratio", 1.0)

    @compression_ratio.setter
    def compression_ratio(self, value: float) -> None:
        self.prompt_compress["compression_ratio"] = value

    # -- prompt_cache --

    @property
    def prompt_cache(self) -> Dict[str, Any]:
        """获取 prompt_cache 命名空间。"""
        if NS_PROMPT_CACHE not in self.extra:
            self.extra[NS_PROMPT_CACHE] = {}
        return self.extra[NS_PROMPT_CACHE]  # type: ignore[no-any-return]

    @prompt_cache.setter
    def prompt_cache(self, value: Dict[str, Any]) -> None:
        self.extra[NS_PROMPT_CACHE] = value

    @property
    def cache_key(self) -> str:
        """L1/L2 缓存键。"""
        return self.prompt_cache.get("cache_key", "")

    @cache_key.setter
    def cache_key(self, value: str) -> None:
        self.prompt_cache["cache_key"] = value

    @property
    def cache_hit(self) -> bool:
        """是否命中缓存。"""
        return self.prompt_cache.get("cache_hit", False)

    @cache_hit.setter
    def cache_hit(self, value: bool) -> None:
        self.prompt_cache["cache_hit"] = value

    # -- semantic_cache --

    @property
    def semantic_cache(self) -> Dict[str, Any]:
        """获取 semantic_cache 命名空间。"""
        if NS_SEMANTIC_CACHE not in self.extra:
            self.extra[NS_SEMANTIC_CACHE] = {}
        return self.extra[NS_SEMANTIC_CACHE]  # type: ignore[no-any-return]

    @semantic_cache.setter
    def semantic_cache(self, value: Dict[str, Any]) -> None:
        self.extra[NS_SEMANTIC_CACHE] = value

    @property
    def similarity_score(self) -> float:
        """语义相似度得分。"""
        return self.semantic_cache.get("similarity_score", 0.0)

    @similarity_score.setter
    def similarity_score(self, value: float) -> None:
        self.semantic_cache["similarity_score"] = value

    @property
    def cached_response(self) -> str:
        """缓存的响应内容。"""
        return self.semantic_cache.get("cached_response", "")

    @cached_response.setter
    def cached_response(self, value: str) -> None:
        self.semantic_cache["cached_response"] = value

    @property
    def collection(self) -> str:
        """Qdrant 集合名。"""
        return self.semantic_cache.get("collection", "semantic_cache")

    @collection.setter
    def collection(self, value: str) -> None:
        self.semantic_cache["collection"] = value

    # -- pii_detector --

    @property
    def pii_detector(self) -> Dict[str, Any]:
        """获取 pii_detector 命名空间。"""
        if NS_PII_DETECTOR not in self.extra:
            self.extra[NS_PII_DETECTOR] = {}
        return self.extra[NS_PII_DETECTOR]  # type: ignore[no-any-return]

    @pii_detector.setter
    def pii_detector(self, value: Dict[str, Any]) -> None:
        self.extra[NS_PII_DETECTOR] = value

    @property
    def detected_categories(self) -> list[str]:
        """检测到的 PII 类别列表。"""
        return self.pii_detector.get("detected_categories", [])

    @detected_categories.setter
    def detected_categories(self, value: list[str]) -> None:
        self.pii_detector["detected_categories"] = value

    @property
    def sanitized_prompt(self) -> str:
        """脱敏后的 prompt。"""
        return self.pii_detector.get("sanitized_prompt", "")

    @sanitized_prompt.setter
    def sanitized_prompt(self, value: str) -> None:
        self.pii_detector["sanitized_prompt"] = value

    # -- model_router --

    @property
    def model_router(self) -> Dict[str, Any]:
        """获取 model_router 命名空间。"""
        if NS_MODEL_ROUTER not in self.extra:
            self.extra[NS_MODEL_ROUTER] = {}
        return self.extra[NS_MODEL_ROUTER]  # type: ignore[no-any-return]

    @model_router.setter
    def model_router(self, value: Dict[str, Any]) -> None:
        self.extra[NS_MODEL_ROUTER] = value

    @property
    def selected_provider(self) -> str:
        """选中的提供商。"""
        return self.model_router.get("selected_provider", "")

    @selected_provider.setter
    def selected_provider(self, value: str) -> None:
        self.model_router["selected_provider"] = value

    @property
    def selected_model(self) -> str:
        """选中的模型。"""
        return self.model_router.get("selected_model", "")

    @selected_model.setter
    def selected_model(self, value: str) -> None:
        self.model_router["selected_model"] = value

    @property
    def fallback_chain(self) -> list[str]:
        """经历的降级链。"""
        return self.model_router.get("fallback_chain", [])

    @fallback_chain.setter
    def fallback_chain(self, value: list[str]) -> None:
        self.model_router["fallback_chain"] = value

    @property
    def circuit_breaker_state(self) -> str:
        """熔断器状态。"""
        return self.model_router.get("circuit_breaker_state", "CLOSED")

    @circuit_breaker_state.setter
    def circuit_breaker_state(self, value: str) -> None:
        self.model_router["circuit_breaker_state"] = value

    # ------------------------------------------------------------------
    # 通用辅助方法
    # ------------------------------------------------------------------

    def mark_stopped(self, reason: str = "") -> None:
        """标记短路，跳过后续插件。

        Args:
            reason: 短路原因，记录到日志。
        """
        self.should_stop = True
        if reason:
            logger.debug("PipelineContext 标记短路: %s", reason)

    def get_plugin_trace(self) -> list[dict[str, Any]]:
        """获取插件执行痕迹（供 _meta.plugin_trace 使用）。

        由 PipelineEngine 在执行过程中累积写入。

        Returns:
            插件执行耗时列表。
        """
        return self.extra.get("_plugin_trace", [])  # type: ignore[no-any-return]

    def add_plugin_trace(self, plugin_name: str, duration_ms: float, status: str) -> None:
        """添加单个插件的执行痕迹。

        Args:
            plugin_name: 插件名称。
            duration_ms: 耗时（毫秒）。
            status: 状态 "success" | "skipped" | "failed"。
        """
        trace = self.get_plugin_trace()
        trace.append({
            "plugin_name": plugin_name,
            "duration_ms": round(duration_ms, 2),
            "status": status,
        })
        self.extra["_plugin_trace"] = trace

    def to_dict(self) -> Dict[str, Any]:
        """将上下文转换为字典（用于调试日志）。

        Returns:
            扁平化的上下文字典副本。
        """
        return {
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "user_id": self.user_id,
            "should_stop": self.should_stop,
            "should_stream": self.should_stream,
            "response_exists": self.response is not None,
            "extra_namespaces": list(self.extra.keys()),
        }
