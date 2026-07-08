"""
OpenAI 兼容接口实现
==================

实现以下接口（API_CONTRACT.md）：
- POST /v1/chat/completions -- 聊天补全（非流式 + 流式）
- GET /v1/models -- 模型列表
- POST /v1/embeddings -- 嵌入向量生成

所有接口需要 API Key 鉴权（由 auth_middleware 处理）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 请求/响应模型
# ------------------------------------------------------------------


class ChatCompletionRequest(BaseModel):
    """POST /v1/chat/completions 请求体。"""

    model: str
    messages: List[Dict[str, Any]]
    temperature: Optional[float] = Field(default=1.0, ge=0.0, le=2.0)
    max_tokens: Optional[int] = None
    top_p: Optional[float] = Field(default=1.0, ge=0.0, le=1.0)
    frequency_penalty: Optional[float] = Field(default=0.0, ge=-2.0, le=2.0)
    presence_penalty: Optional[float] = Field(default=0.0, ge=-2.0, le=2.0)
    stream: bool = False
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None
    stop: Optional[Any] = None
    user: Optional[str] = None
    # 显式生成意图开关(classify_request 据此分流到 generation 管道)
    generation_intent: Optional[bool] = False


class EmbeddingRequest(BaseModel):
    """POST /v1/embeddings 请求体。"""

    model: str
    input: str | List[str]
    user: Optional[str] = None


# ------------------------------------------------------------------
# 辅助函数
# ------------------------------------------------------------------
# Cost estimation, stream metrics wrapper, L3 vector computation, and L3
# async backfill moved to core in Task 5 (runtime-structure refactor):
#   - _estimate_cost              → aigateway_core.route.metrics.costing
#   - _wrap_stream_for_metrics    → aigateway_core.route.streaming.metrics_wrapper
#   - _compute_l3_vector          → aigateway_core.prefix.cache.l3_semantic
#   - _safe_l3_backfill           → aigateway_core.prefix.cache.l3_semantic
# Re-exported here so existing callers (including the core dispatcher's
# lazy imports, which now point at the core modules directly) keep working.

from aigateway_core.route.metrics.costing import _estimate_cost
from aigateway_core.route.streaming.metrics_wrapper import _wrap_stream_for_metrics
from aigateway_core.prefix.cache.l3_semantic import (
    _compute_l3_vector,
    _l3_model_cache,
    _safe_l3_backfill,
)


def _get_app_state() -> Dict[str, Any]:
    """从 FastAPI app.state 获取全局组件。"""
    from aigateway_api.main import app
    s = app.state
    return {
        "cache_manager": getattr(s, "cache_manager"),
        "key_store": getattr(s, "key_store"),
        "litellm_bridge": getattr(s, "litellm_bridge"),
        "metrics_collector": getattr(s, "metrics_collector"),
        "config_manager": getattr(s, "config_manager"),
        "plugin_registry": getattr(s, "plugin_registry"),
        "redis_manager": getattr(s, "redis_manager"),
        "qdrant_manager": getattr(s, "qdrant_manager"),
        "media_optimization_layer": getattr(s, "media_optimization_layer", None),
        "media_cache": getattr(s, "media_cache", None),
        "pii_detector_plugin": getattr(s, "pii_detector_plugin", None),
        "model_router_resolver": getattr(s, "model_router_resolver", None),
        "prompt_compress_plugin": getattr(s, "prompt_compress_plugin", None),
        # 两条管道的 PipelineEngine —— RequestDispatcher.__init__ 用 state.get()
        # 拿它们跑插件链。缺失会让 dispatcher 静默跳过整条 engine 循环
        # (understanding 侧无 rag/conv,generation 侧 6 个 gen-opt 插件全部
        # 不执行),trace 事件里插件事件全部缺失但没有任何报错日志。
        "understanding_engine": getattr(s, "understanding_engine", None),
        "generation_engine": getattr(s, "generation_engine", None),
    }


def _get_redis_client() -> Any:
    """获取 Redis 客户端（跨 worker 共享）。

    多 worker 模式下 app.state 不共享，
    直接从环境变量连接 Redis 保证每个 worker 都有独立的连接。
    """
    import os
    import redis.asyncio as redis

    url = os.environ.get("AI_GATEWAY_REDIS_URL", "redis://localhost:6379/0")
    try:
        r = redis.from_url(url, decode_responses=False)
        return r
    except Exception:
        return None


# L3 向量计算 + 异步回填已移至 aigateway_core.prefix.cache.l3_semantic
# (Task 5 runtime-structure refactor)，见本节顶部 import。


async def _record_request_log(
    request: Request,
    method: str,
    endpoint: str,
    status_code: int,
    duration_ms: float,
    model: str,
    cache_hit: bool,
    cache_tier: Optional[str],
) -> None:
    """记录请求日志到 Redis ZSET，供前端 /admin/logs 查询。"""
    import time
    import uuid

    # 从 request.state 获取 request_id/trace_id/user_id
    request_id = getattr(request.state, "request_id", "") or str(uuid.uuid4().hex[:12])
    trace_id = getattr(request.state, "trace_id", "")
    # 不再 fallback mint —— TraceMiddleware 已保证 request.state.trace_id 一定存在
    user_id = getattr(request.state, "user_id", "") or ""
    if not user_id:
        api_key_data = getattr(request.state, "api_key_data", None)
        if api_key_data:
            user_id = api_key_data.get("user_id", "")

    # 获取 plugin_trace（管线插件执行步骤）
    plugin_trace = getattr(request.state, "plugin_trace", None) or []

    redis_client = _get_redis_client()
    if redis_client is None:
        return
    now = time.time()
    log_entry = json.dumps({
        "request_id": request_id,
        "trace_id": trace_id,
        "user_id": user_id,
        "method": method,
        "endpoint": endpoint,
        "status": status_code,
        "duration_ms": round(duration_ms, 1),
        "model": model,
        "cache_hit": cache_hit,
        "tier": cache_tier,
        "timestamp": now,
        "plugin_trace": plugin_trace,
    }, ensure_ascii=False)
    try:
        await redis_client.zadd("aigateway:logs:requests", {log_entry: now})
        await redis_client.zremrangebyrank("aigateway:logs:requests", 0, -10001)
    except Exception:
        pass  # Non-critical: don't fail the request if logging fails


# ------------------------------------------------------------------
# Media Optimization (V2) — 在 LLM 调用前处理多模态内容
# ------------------------------------------------------------------


async def _apply_media_optimization(
    body: "ChatCompletionRequest",
    request: Request,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """对请求消息应用 Media Optimization Layer。

    检测并处理多模态内容（图片 OCR、音频转录等），
    将媒体转为文本以节约 token。

    Returns:
        {"messages": optimized_messages, "meta": {...}}
        失败或无多模态内容时原样返回。
    """
    mol_plugin = state.get("media_optimization_layer")
    result: Dict[str, Any] = {"messages": body.messages, "meta": {}}

    if mol_plugin is None:
        return result

    # 仅当消息包含 list 类型 content（多模态）时才处理
    has_multimodal = any(
        isinstance(m.get("content"), list) for m in body.messages
    )
    if not has_multimodal:
        return result

    try:
        from aigateway_core.context import PipelineContext

        ctx = PipelineContext(request={"messages": body.messages, "model": body.model},
                              trace_id=request.state.trace_id)
        if hasattr(request.state, "user_id"):
            ctx.user_id = request.state.user_id

        ctx = await mol_plugin.execute(ctx)

        optimized_messages = ctx.request.get("messages", body.messages)
        mol_ns = ctx.extra.get("media_optimization", {})
        result["messages"] = optimized_messages
        result["meta"] = {
            "is_multimodal": ctx.is_multimodal,
            "detected_types": mol_ns.get("detected_types", []),
            "token_savings": ctx.total_token_savings,
            "processors_executed": mol_ns.get("processors_executed", []),
        }
        logger.info(
            "Media optimization applied: types=%s, savings=%d",
            mol_ns.get("detected_types", []),
            ctx.total_token_savings,
        )
    except Exception as exc:
        logger.warning("Media optimization 失败（原样透传）: %s", exc)

    return result


# ------------------------------------------------------------------
# PII Detection
# ------------------------------------------------------------------


async def _apply_pii_detection(
    body: "ChatCompletionRequest",
    request: Request,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """Apply PII detection and sanitization to the request.

    Returns:
        {"messages": sanitized_messages, "meta": {...}}
        If reject strategy triggers, returns {"error": {...}, "status_code": 403}.
    """
    pii_plugin = state.get("pii_detector_plugin")
    result: Dict[str, Any] = {"messages": body.messages, "meta": {}}

    if pii_plugin is None:
        return result

    try:
        from aigateway_core.context import PipelineContext

        ctx = PipelineContext(request={"messages": body.messages, "model": body.model},
                              trace_id=request.state.trace_id)
        if hasattr(request.state, "user_id"):
            ctx.user_id = request.state.user_id

        ctx = await pii_plugin.execute(ctx)

        pii_ns = ctx.pii_detector
        result["meta"] = {
            "has_pii": pii_ns.get("has_pii", False),
            "detected_categories": pii_ns.get("detected_categories", []),
            "strategy": pii_ns.get("strategy", "sanitize"),
        }

        # If reject strategy triggered, return 403
        if pii_ns.get("error"):
            return {"error": {"code": "pii_rejected", "message": pii_ns["error"]}, "status_code": 403}

        # Use sanitized messages from context (updated by PIIDetectorPlugin.execute)
        sanitized_messages = ctx.request.get("messages", body.messages)
        if sanitized_messages:
            result["messages"] = sanitized_messages

        logger.info(
            "PII detection applied: has_pii=%s, categories=%s",
            pii_ns.get("has_pii", False),
            pii_ns.get("detected_categories", []),
        )
    except Exception as exc:
        logger.warning("PII detection failed (pass-through): %s", exc)

    return result


# ------------------------------------------------------------------
# Model Router (auto resolution)
# ------------------------------------------------------------------


async def _resolve_auto_model(
    body: "ChatCompletionRequest",
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """Resolve model='auto' to the best available provider/model.

    Returns:
        {"model": resolved_model_name, "meta": {...}}
        If model is not "auto", returns {"model": body.model, "meta": {}}.
    """
    if body.model != "auto":
        return {"model": body.model, "meta": {}}

    resolver = state.get("model_router_resolver")
    litellm_bridge = state.get("litellm_bridge")

    # Try full ModelRouterStrategy
    if resolver is not None:
        try:
            # Estimate complexity from prompt length
            full_text_parts: list[str] = []
            for m in body.messages:
                content = m.get("content", "")
                if isinstance(content, str):
                    full_text_parts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            full_text_parts.append(block.get("text", ""))
            full_text = " ".join(full_text_parts)
            complexity_score = min(100, max(0, len(full_text) // 50))

            decision = await resolver.route(
                complexity_score=complexity_score,
                required_modality="llm",
            )
            resolved_model = decision.selected_model
            meta = {
                "selected_model": resolved_model,
                "selected_provider": decision.selected_provider,
                "reason": decision.reason,
                "estimated_cost": decision.estimated_cost,
            }
            logger.info("Auto model routed: %s -> %s (reason=%s)", body.model, resolved_model, decision.reason)
            return {"model": resolved_model, "meta": meta}
        except Exception as exc:
            logger.warning("ModelRouterStrategy failed, falling back to cheapest: %s", exc)

    # Fallback: pick first available model from litellm_bridge
    if litellm_bridge is not None:
        try:
            registered = litellm_bridge.get_registered_models()
            if registered:
                resolved_model = registered[0]
                logger.info("Auto model resolved (fallback): %s -> %s", body.model, resolved_model)
                return {"model": resolved_model, "meta": {
                    "selected_model": resolved_model,
                    "selected_provider": "fallback",
                    "reason": "auto_fallback",
                }}
        except Exception as exc:
            logger.warning("Auto model fallback failed: %s", exc)

    # Final fallback: error
    return {
        "error": {
            "code": "model_not_found",
            "message": "'auto' model resolution failed: no providers configured",
        },
        "status_code": 400,
    }


# ------------------------------------------------------------------
# Prompt Compression
# ------------------------------------------------------------------


async def _apply_prompt_compression(
    body: "ChatCompletionRequest",
    request: Request,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """Apply prompt compression before LLM call.

    Returns:
        {"messages": updated_messages, "meta": {...}}
        If plugin unavailable or passthrough, returns original messages.
    """
    compress_plugin = state.get("prompt_compress_plugin")
    result: Dict[str, Any] = {"messages": body.messages, "meta": {}}

    if compress_plugin is None:
        return result

    try:
        from aigateway_core.context import PipelineContext

        ctx = PipelineContext(request={"messages": body.messages, "model": body.model},
                              trace_id=request.state.trace_id)
        ctx = await compress_plugin.execute(ctx)

        pc_ns = ctx.prompt_compress
        result["meta"] = {
            "original_tokens": pc_ns.get("original_tokens", 0),
            "compressed_tokens": pc_ns.get("compressed_tokens", 0),
            "compression_ratio": pc_ns.get("compression_ratio", 1.0),
        }

        # Update messages if compression produced a result
        new_messages = ctx.request.get("messages")
        if new_messages:
            result["messages"] = new_messages

        if result["meta"]["compression_ratio"] < 1.0:
            logger.info(
                "Prompt compression applied: %.1f%% reduction",
                (1 - result["meta"]["compression_ratio"]) * 100,
            )
    except Exception as exc:
        logger.warning("Prompt compression failed (pass-through): %s", exc)

    return result


# ------------------------------------------------------------------
# POST /v1/chat/completions
# ------------------------------------------------------------------
# 请求处理由 RequestDispatcher 承担（总分总架构：分流 → 管道插件链 → LiteLLM 出口）。
# dispatcher 复用的辅助函数分布：
#   - 仍在 API surface: _apply_media_optimization / _apply_pii_detection /
#     _resolve_auto_model / _apply_prompt_compression / _record_request_log
#   - 已移至 core (Task 5): _estimate_cost / _compute_l3_vector / _safe_l3_backfill
#     (aigateway_core.route.metrics / aigateway_core.prefix.cache.l3_semantic)
# 详见 aigateway_core.dispatch.dispatcher (core) + aigateway_api/dispatcher.py (shim)。


# ------------------------------------------------------------------
# GET /v1/models
# ------------------------------------------------------------------


async def list_models(request: Request) -> JSONResponse:
    """列出可用模型。"""
    state = _get_app_state()
    litellm_bridge = state.get("litellm_bridge")

    if litellm_bridge is None:
        # 无 LiteLLM 时返回空列表
        return JSONResponse(content={
            "data": {"object": "list", "data": []},
            "message": "success",
        })

    try:
        models = await litellm_bridge.list_models()
    except Exception as exc:
        logger.error("Failed to list models: %s", exc)
        return JSONResponse(
            content={"error": {"code": "internal_error", "message": f"Failed to fetch model list: {exc}"}},
            status_code=500,
        )

    return JSONResponse(content={
        "object": "list",
        "data": models,
    })


# ------------------------------------------------------------------
# POST /v1/embeddings
# ------------------------------------------------------------------


async def create_embeddings(
    body: EmbeddingRequest,
    request: Request,
) -> JSONResponse:
    """生成嵌入向量。

    支持 sentence-transformers（本地）和 OpenAI API（云端）后端。
    """
    # 验证输入
    if not isinstance(body.input, (str, list)):
        return JSONResponse(
            content={"error": {"code": "validation_error", "message": "Input must be a string or array of strings"}},
            status_code=400,
        )

    input_texts = body.input if isinstance(body.input, list) else [body.input]

    if not input_texts or not any(t.strip() for t in input_texts):
        return JSONResponse(
            content={"error": {"code": "validation_error", "message": "Input must not be empty"}},
            status_code=400,
        )

    # 获取配置中的 embedding 后端
    state = _get_app_state()
    config_manager = state.get("config_manager")
    embedding_backend = "sentence_transformers"
    embedding_model = body.model or "all-MiniLM-L6-v2"

    if config_manager:
        emb_cfg = config_manager.get("embedding", {})
        if emb_cfg:
            embedding_backend = emb_cfg.get("backend", "sentence_transformers")
            if not body.model:
                embedding_model = emb_cfg.get("model", "all-MiniLM-L6-v2")

    # sentence-transformers 本地后端
    if embedding_backend == "sentence_transformers":
        try:
            from sentence_transformers import SentenceTransformer
            # 模块级缓存，避免每请求加载模型
            if not hasattr(openai_compat, "_st_model_cache"):
                openai_compat._st_model_cache: Dict[str, Any] = {}  # type: ignore[attr-defined]
            _cache = getattr(openai_compat, "_st_model_cache")  # type: ignore[attr-defined]
            st_model = _cache.get(embedding_model)
            if st_model is None:
                st_model = SentenceTransformer(embedding_model)
                _cache[embedding_model] = st_model
            embeddings = st_model.encode(input_texts, normalize_embeddings=True)
        except ImportError:
            return JSONResponse(
                content={"error": {"code": "unsupported", "message": "sentence-transformers not installed"}},
                status_code=501,
            )
        except Exception as exc:
            return JSONResponse(
                content={"error": {"code": "invalid_model", "message": f"Embedding model '{embedding_model}' not found: {exc}"}},
                status_code=400,
            )

        data_items = []
        if isinstance(embeddings, list):
            for i, emb in enumerate(embeddings):
                data_items.append({
                    "object": "embedding",
                    "index": i,
                    "embedding": emb.tolist() if hasattr(emb, "tolist") else list(emb),
                })
        else:
            data_items.append({
                "object": "embedding",
                "index": 0,
                "embedding": embeddings.tolist() if hasattr(embeddings, "tolist") else list(embeddings),
            })

        return JSONResponse(content={
            "data": {
                "object": "list",
                "data": data_items,
                "usage": {
                    "prompt_tokens": sum(len(t) for t in input_texts),
                    "total_tokens": sum(len(t) for t in input_texts),
                },
            },
            "message": "success",
        })

    # OpenAI API 后端
    if embedding_backend == "openai":
        try:
            import httpx

            api_key = state.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")
            if not api_key:
                return JSONResponse(
                    content={"error": {"code": "internal_error", "message": "OpenAI API key not configured"}},
                    status_code=500,
                )

            model_name = body.model or "text-embedding-3-small"
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/embeddings",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model_name,
                        "input": input_texts,
                    },
                )
                resp.raise_for_status()
                openai_data = resp.json()

            # 转换 OpenAI 响应格式
            openai_items = openai_data.get("data", [])
            data_items = []
            for i, item in enumerate(openai_items):
                data_items.append({
                    "object": "embedding",
                    "index": item.get("index", i),
                    "embedding": item.get("embedding", []),
                })

            usage = openai_data.get("usage", {})
            return JSONResponse(content={
                "data": {
                    "object": "list",
                    "data": data_items,
                    "usage": {
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0),
                    },
                },
                "message": "success",
            })

        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            body_text = exc.response.text
            return JSONResponse(
                content={"error": {"code": "upstream_error", "message": f"OpenAI API error: {status} {body_text}"}},
                status_code=502,
            )
        except Exception as exc:
            return JSONResponse(
                content={"error": {"code": "internal_error", "message": f"OpenAI embedding failed: {exc}"}},
                status_code=500,
            )

    # 未知后端
    return JSONResponse(
        content={"error": {"code": "unsupported", "message": f"Unknown embedding backend: {embedding_backend}"}},
        status_code=400,
    )


# ------------------------------------------------------------------
# 路由挂载
# ------------------------------------------------------------------


def _setup_router() -> Any:
    """创建并配置 FastAPI router。"""
    from fastapi import APIRouter, Depends, Request
    from .auth_middleware import authenticate

    router_obj = APIRouter()

    @router_obj.post("/chat/completions")
    async def post_chat_completions(
        body: ChatCompletionRequest,
        request: Request,
        _auth: Dict[str, Any] = Depends(authenticate),
    ):
        # 总分总架构：所有请求经 RequestDispatcher 分流到理解/生成管道，
        # 两条管道跑完插件链后统一从 LiteLLMBridge 出口调下游。
        from aigateway_api.dispatcher import RequestDispatcher
        state = _get_app_state()
        dispatcher = RequestDispatcher(state)
        return await dispatcher.dispatch(body, request)

    @router_obj.get("/models")
    async def get_models(request: Request, _auth: Dict[str, Any] = Depends(authenticate)):
        return await list_models(request)

    @router_obj.post("/embeddings")
    async def post_embeddings(
        body: EmbeddingRequest,
        request: Request,
        _auth: Dict[str, Any] = Depends(authenticate),
    ):
        return await create_embeddings(body, request)

    return router_obj


# 模块级 router 实例（由 main.py 使用）
router = _setup_router()
