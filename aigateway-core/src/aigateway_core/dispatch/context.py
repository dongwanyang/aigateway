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
NS_MEDIA_OPTIMIZATION = "media_optimization"
NS_GENERATION_PIPELINE = "generation_pipeline"
NS_RAG_RETRIEVER = "rag_retriever"
NS_CONV_COMPRESSOR = "conv_compressor"


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
        trace_id: 全请求唯一追踪 ID（由 TraceMiddleware 生成,必须显式传入）。
        request_id: 唯一请求 ID（UUID4）。
        user_id: 从 API Key 解析出的用户 ID。
        extra: 插件间传递的命名空间数据字典。

    Note:
        Do NOT use ``copy.copy(ctx)`` — the ``default_factory`` for
        ``request_id`` will not be re-invoked, causing shared IDs across
        copies.  Create a new instance via the constructor instead.
    """

    request: Dict[str, Any]
    trace_id: str
    response: Optional[str] = None
    should_stop: bool = False
    should_stream: bool = False
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    user_id: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)
    pipeline_kind: str = "understanding"
    is_multimodal: bool = False
    total_token_savings: int = 0

    @property
    def prompt_compress(self) -> Dict[str, Any]:
        if NS_PROMPT_COMPRESS not in self.extra:
            self.extra[NS_PROMPT_COMPRESS] = {}
        return self.extra[NS_PROMPT_COMPRESS]  # type: ignore[no-any-return]

    @prompt_compress.setter
    def prompt_compress(self, value: Dict[str, Any]) -> None:
        self.extra[NS_PROMPT_COMPRESS] = value

    @property
    def original_length(self) -> int:
        return self.prompt_compress.get("original_length", 0)

    @original_length.setter
    def original_length(self, value: int) -> None:
        self.prompt_compress["original_length"] = value

    @property
    def compressed_prompt(self) -> str:
        return self.prompt_compress.get("compressed_prompt", "")

    @compressed_prompt.setter
    def compressed_prompt(self, value: str) -> None:
        self.prompt_compress["compressed_prompt"] = value

    @property
    def compression_ratio(self) -> float:
        return self.prompt_compress.get("compression_ratio", 1.0)

    @compression_ratio.setter
    def compression_ratio(self, value: float) -> None:
        self.prompt_compress["compression_ratio"] = value

    @property
    def prompt_cache(self) -> Dict[str, Any]:
        if NS_PROMPT_CACHE not in self.extra:
            self.extra[NS_PROMPT_CACHE] = {}
        return self.extra[NS_PROMPT_CACHE]  # type: ignore[no-any-return]

    @prompt_cache.setter
    def prompt_cache(self, value: Dict[str, Any]) -> None:
        self.extra[NS_PROMPT_CACHE] = value

    @property
    def cache_key(self) -> str:
        return self.prompt_cache.get("cache_key", "")

    @cache_key.setter
    def cache_key(self, value: str) -> None:
        self.prompt_cache["cache_key"] = value

    @property
    def cache_hit(self) -> bool:
        return self.prompt_cache.get("cache_hit", False)

    @cache_hit.setter
    def cache_hit(self, value: bool) -> None:
        self.prompt_cache["cache_hit"] = value

    @property
    def semantic_cache(self) -> Dict[str, Any]:
        if NS_SEMANTIC_CACHE not in self.extra:
            self.extra[NS_SEMANTIC_CACHE] = {}
        return self.extra[NS_SEMANTIC_CACHE]  # type: ignore[no-any-return]

    @semantic_cache.setter
    def semantic_cache(self, value: Dict[str, Any]) -> None:
        self.extra[NS_SEMANTIC_CACHE] = value

    @property
    def similarity_score(self) -> float:
        return self.semantic_cache.get("similarity_score", 0.0)

    @similarity_score.setter
    def similarity_score(self, value: float) -> None:
        self.semantic_cache["similarity_score"] = value

    @property
    def cached_response(self) -> str:
        return self.semantic_cache.get("cached_response", "")

    @cached_response.setter
    def cached_response(self, value: str) -> None:
        self.semantic_cache["cached_response"] = value

    @property
    def collection(self) -> str:
        return self.semantic_cache.get("collection", "semantic_cache")

    @collection.setter
    def collection(self, value: str) -> None:
        self.semantic_cache["collection"] = value

    @property
    def pii_detector(self) -> Dict[str, Any]:
        if NS_PII_DETECTOR not in self.extra:
            self.extra[NS_PII_DETECTOR] = {}
        return self.extra[NS_PII_DETECTOR]  # type: ignore[no-any-return]

    @pii_detector.setter
    def pii_detector(self, value: Dict[str, Any]) -> None:
        self.extra[NS_PII_DETECTOR] = value

    @property
    def detected_categories(self) -> list[str]:
        return self.pii_detector.get("detected_categories", [])

    @detected_categories.setter
    def detected_categories(self, value: list[str]) -> None:
        self.pii_detector["detected_categories"] = value

    @property
    def sanitized_prompt(self) -> str:
        return self.pii_detector.get("sanitized_prompt", "")

    @sanitized_prompt.setter
    def sanitized_prompt(self, value: str) -> None:
        self.pii_detector["sanitized_prompt"] = value

    @property
    def model_router(self) -> Dict[str, Any]:
        if NS_MODEL_ROUTER not in self.extra:
            self.extra[NS_MODEL_ROUTER] = {}
        return self.extra[NS_MODEL_ROUTER]  # type: ignore[no-any-return]

    @model_router.setter
    def model_router(self, value: Dict[str, Any]) -> None:
        self.extra[NS_MODEL_ROUTER] = value

    @property
    def selected_provider(self) -> str:
        return self.model_router.get("selected_provider", "")

    @selected_provider.setter
    def selected_provider(self, value: str) -> None:
        self.model_router["selected_provider"] = value

    @property
    def selected_model(self) -> str:
        return self.model_router.get("selected_model", "")

    @selected_model.setter
    def selected_model(self, value: str) -> None:
        self.model_router["selected_model"] = value

    @property
    def fallback_chain(self) -> list[str]:
        return self.model_router.get("fallback_chain", [])

    @fallback_chain.setter
    def fallback_chain(self, value: list[str]) -> None:
        self.model_router["fallback_chain"] = value

    @property
    def circuit_breaker_state(self) -> str:
        return self.model_router.get("circuit_breaker_state", "CLOSED")

    @circuit_breaker_state.setter
    def circuit_breaker_state(self, value: str) -> None:
        self.model_router["circuit_breaker_state"] = value

    @property
    def media_optimization(self) -> Dict[str, Any]:
        if NS_MEDIA_OPTIMIZATION not in self.extra:
            self.extra[NS_MEDIA_OPTIMIZATION] = {
                "detected_types": [],
                "processors_executed": [],
                "total_savings": 0,
                "per_media_results": [],
            }
        return self.extra[NS_MEDIA_OPTIMIZATION]

    @media_optimization.setter
    def media_optimization(self, value: Dict[str, Any]) -> None:
        self.extra[NS_MEDIA_OPTIMIZATION] = value

    @property
    def generation_pipeline(self) -> Dict[str, Any]:
        if NS_GENERATION_PIPELINE not in self.extra:
            self.extra[NS_GENERATION_PIPELINE] = {
                "prompt_enhanced": False,
                "enhancement_level": "off",
                "selected_model": "",
                "completion_tokens": 0,
                "prompt_tokens": 0,
            }
        return self.extra[NS_GENERATION_PIPELINE]

    @generation_pipeline.setter
    def generation_pipeline(self, value: Dict[str, Any]) -> None:
        self.extra[NS_GENERATION_PIPELINE] = value

    @property
    def rag_context(self) -> list:
        return self.extra.get(NS_RAG_RETRIEVER, {}).get("retrieved_chunks", [])

    @property
    def conv_summary(self) -> str:
        return self.extra.get(NS_CONV_COMPRESSOR, {}).get("summary", "")

    def mark_stopped(self, reason: str = "") -> None:
        self.should_stop = True
        if reason:
            logger.debug("PipelineContext 标记短路: %s", reason)

    def get_plugin_trace(self) -> list[dict[str, Any]]:
        return self.extra.get("_plugin_trace", [])  # type: ignore[no-any-return]

    def add_plugin_trace(self, plugin_name: str, duration_ms: float,
                         status: str = "ok", payload: dict | None = None) -> None:
        """插件执行完毕后由插件自身调用,补充一条 kind=plugin 事件。

        PipelineEngine.execute_ctx 已自动为每个插件 emit 了 plugin 事件,
        此方法供插件在需要携带额外 metadata 时调用(如 RAG 检索条数、
        压缩比例、意图分类结果等)。
        """
        # 1) 同步更新 _plugin_trace 列表（向后兼容 request.state.plugin_trace / _meta）
        trace = self.get_plugin_trace()
        trace.append({
            "plugin_name": plugin_name,
            "duration_ms": round(duration_ms, 2),
            "status": status,
        })
        self.extra["_plugin_trace"] = trace

        # 2) 发 TraceEvent（给 Redis trace 和 admin/trace 接口用）
        import time as _time
        from aigateway_core.shared.trace_event import TraceCollector, TraceEvent
        collector = TraceCollector.current()
        if collector:
            norm_status = (
                "ok" if status == "success"
                else ("error" if status == "failed" else "skip")
            )
            collector.emit(TraceEvent(
                trace_id=self.trace_id,
                ts=_time.monotonic(),
                stage=plugin_name,
                kind="plugin",
                name=f"{plugin_name}.execute",
                duration_ms=round(duration_ms, 2),
                status=norm_status,
                payload=payload,
            ))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "user_id": self.user_id,
            "should_stop": self.should_stop,
            "should_stream": self.should_stream,
            "response_exists": self.response is not None,
            "extra_namespaces": list(self.extra.keys()),
        }
