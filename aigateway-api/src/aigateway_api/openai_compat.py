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


class EmbeddingRequest(BaseModel):
    """POST /v1/embeddings 请求体。"""

    model: str
    input: str | List[str]
    user: Optional[str] = None


# ------------------------------------------------------------------
# 辅助函数
# ------------------------------------------------------------------


def _estimate_cost(model: str, total_tokens: int) -> float:
    """根据模型和 token 数估算成本（美元）。

    与 litellm_bridge._estimate_cost() 定价表保持一致。
    """
    pricing = {
        "gpt-4o": 0.000005,
        "gpt-4o-mini": 0.00000015,
        "claude-3-5-sonnet": 0.000003,
        "claude-3-haiku": 0.00000025,
        "gemini-1.5-pro": 0.0000025,
        "agnes-2.0-flash": 0.0000005,
    }
    base = model.split("/")[-1] if "/" in model else model
    return round(total_tokens * pricing.get(base, 0.000001), 6)


async def _wrap_stream_for_metrics(
    completion_gen: Any,
    metrics_collector: Any,
    model: str,
    user_id: str = "",
) -> Any:
    """包装流式生成器，从最后一个 chunk 提取 usage 并记录指标。"""
    last_chunk: Dict[str, Any] = {}
    async for chunk in completion_gen:
        last_chunk = chunk
        yield chunk

    # 从最后一个 chunk 提取 usage 数据
    usage = last_chunk.get("usage", {})
    if not usage:
        return

    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    total_tokens = usage.get("total_tokens", 0)

    if prompt_tokens > 0:
        metrics_collector.record_tokens(prompt_tokens, "prompt")
    if completion_tokens > 0:
        metrics_collector.record_tokens(completion_tokens, "completion")
    if total_tokens > 0:
        cost = _estimate_cost(model, total_tokens)
        if cost > 0:
            metrics_collector.record_cost(cost, model=model, user_id=user_id)


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
        "circuit_breaker_factory": getattr(s, "circuit_breaker_factory"),
        "redis_manager": getattr(s, "redis_manager"),
        "qdrant_manager": getattr(s, "qdrant_manager"),
        "media_optimization_layer": getattr(s, "media_optimization_layer", None),
        "media_cache": getattr(s, "media_cache", None),
        "pii_detector_plugin": getattr(s, "pii_detector_plugin", None),
        "model_router_resolver": getattr(s, "model_router_resolver", None),
        "prompt_compress_plugin": getattr(s, "prompt_compress_plugin", None),
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


# ------------------------------------------------------------------
# L3 向量计算 — Qwen/Qwen3-Embedding-0.6B (1024 维)
# ------------------------------------------------------------------

# 模块级模型缓存（避免每次请求加载 ~600MB 模型）
_l3_model_cache: Dict[str, Any] = {}


async def _compute_l3_vector(text: str) -> Optional[list]:
    """使用 Qwen/Qwen3-Embedding-0.6B 计算 1024 维 embedding 向量。

    使用 transformers + torch 直接加载（无需 sentence_transformers）。
    模型在首次调用时加载并缓存到模块级变量。

    Args:
        text: 待嵌入的文本（通常是 normalized_messages）。

    Returns:
        1024 维归一化向量列表，失败返回 None。
    """
    try:
        import torch
        from transformers import AutoModel, AutoTokenizer

        model_name = "Qwen/Qwen3-Embedding-0.6B"

        # 从模块级缓存获取或加载模型
        if "tokenizer" not in _l3_model_cache:
            logger.info("Loading L3 embedding model: %s", model_name)
            _l3_model_cache["tokenizer"] = AutoTokenizer.from_pretrained(
                model_name, trust_remote_code=True
            )
            _l3_model_cache["model"] = AutoModel.from_pretrained(
                model_name, trust_remote_code=True
            ).eval()
            logger.info("L3 embedding model loaded successfully")

        tokenizer = _l3_model_cache["tokenizer"]
        model = _l3_model_cache["model"]

        # Tokenize（截断过长文本）
        inputs = tokenizer(
            text, return_tensors="pt", truncation=True, max_length=512, padding=True
        )

        # 推理
        with torch.no_grad():
            outputs = model(**inputs)

        # 使用 last_hidden_state 的 mean pooling 作为 sentence embedding
        attention_mask = inputs["attention_mask"]
        token_embeddings = outputs.last_hidden_state  # (1, seq_len, 1024)
        input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
        embedding = torch.sum(token_embeddings * input_mask_expanded, 1) / torch.clamp(
            input_mask_expanded.sum(1), min=1e-9
        )

        # L2 归一化
        embedding = torch.nn.functional.normalize(embedding, p=2, dim=1)

        return embedding[0].tolist()

    except ImportError as exc:
        logger.warning("L3 vector: transformers/torch not available: %s", exc)
        return None
    except Exception as exc:
        logger.warning("L3 vector computation failed: %s", exc)
        return None


async def _safe_l3_backfill(
    cache_manager: Any,
    cache_key: str,
    value_str: str,
    normalized_messages: str,
    model: str,
    user_id: str,
    token_count: int,
) -> None:
    """异步回填 L3 语义缓存（fire-and-forget）。

    使用 Qwen/Qwen3-Embedding-0.6B (transformers) 计算 1024 维向量后存入 Qdrant。
    失败时仅记录 WARNING，不影响主请求。
    """
    try:
        if cache_manager._qdrant_client is None:
            return

        # 计算 embedding 向量
        vector = await _compute_l3_vector(normalized_messages)
        if vector is None:
            return

        # 存入 Qdrant
        await cache_manager.l3_store(
            prompt_hash=cache_key,
            prompt_normalized=normalized_messages[:500],  # 截断避免 payload 过大
            model=model,
            response_json=value_str,
            user_id=user_id,
            token_count=token_count,
            vector=vector,
        )
        logger.debug("L3 backfill success: key=%s", cache_key[:16])
    except Exception as exc:
        logger.warning("L3 backfill failed: %s", exc)


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
    trace_id = getattr(request.state, "trace_id", "") or str(uuid.uuid4().hex[:12])
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

        ctx = PipelineContext(request={"messages": body.messages, "model": body.model})
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

        ctx = PipelineContext(request={"messages": body.messages, "model": body.model})
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

        ctx = PipelineContext(request={"messages": body.messages, "model": body.model})
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
# POST /v1/chat/completions -- 非流式
# ------------------------------------------------------------------


async def chat_completions_non_stream(
    body: ChatCompletionRequest,
    request: Request,
) -> JSONResponse:
    """非流式聊天补全响应。

    流程: 缓存检查 → 配额检查 → 下游 LLM 调用 → 用量记录 → 缓存回填
    """
    request_start_time = time.time()
    plugin_trace: list = []  # 手动记录各步骤耗时

    state = _get_app_state()
    cache_manager = state["cache_manager"]
    key_store = state["key_store"]
    litellm_bridge = state.get("litellm_bridge")
    metrics_collector = state["metrics_collector"]

    # 解析 user_id / key_hash（从鉴权中间件注入）
    user_id: Optional[str] = None
    key_hash: Optional[str] = None
    if hasattr(request.state, "api_key_data"):
        key_data = request.state.api_key_data
        if key_data:
            user_id = key_data.get("user_id") or None
            raw_key = getattr(request.state, "api_key_value", "")
            if raw_key:
                key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
    if not user_id and hasattr(request.state, "user_id"):
        user_id = request.state.user_id

    # ===== Media Optimization (V2) — 处理多模态内容 =====
    mol_start = time.time()
    mol_result = await _apply_media_optimization(body, request, state)
    body.messages = mol_result["messages"]
    mol_meta = mol_result["meta"]
    mol_ms = round((time.time() - mol_start) * 1000, 2)
    if mol_meta:
        plugin_trace.append({"plugin_name": "media_optimization", "duration_ms": mol_ms, "status": "success"})

    # ===== PII Detection =====
    pii_start = time.time()
    pii_result = await _apply_pii_detection(body, request, state)
    if "error" in pii_result:
        pii_ms = round((time.time() - pii_start) * 1000, 2)
        plugin_trace.append({"plugin_name": "pii_detector", "duration_ms": pii_ms, "status": "rejected"})
        request.state.plugin_trace = plugin_trace
        await _record_request_log(
            request=request, method="POST", endpoint="/v1/chat/completions",
            status_code=403, duration_ms=0, model=body.model,
            cache_hit=False, cache_tier=None,
        )
        return JSONResponse(
            content=pii_result["error"],
            status_code=403,
        )
    body.messages = pii_result["messages"]
    pii_meta = pii_result["meta"]
    pii_ms = round((time.time() - pii_start) * 1000, 2)
    if pii_meta:
        plugin_trace.append({"plugin_name": "pii_detector", "duration_ms": pii_ms, "status": "success", **pii_meta})

    # ===== Model Router (auto) =====
    router_start = time.time()
    router_result = await _resolve_auto_model(body, state)
    if "error" in router_result:
        router_ms = round((time.time() - router_start) * 1000, 2)
        plugin_trace.append({"plugin_name": "model_router", "duration_ms": router_ms, "status": "failed"})
        request.state.plugin_trace = plugin_trace
        await _record_request_log(
            request=request, method="POST", endpoint="/v1/chat/completions",
            status_code=400, duration_ms=0, model=body.model,
            cache_hit=False, cache_tier=None,
        )
        return JSONResponse(
            content=router_result["error"],
            status_code=400,
        )
    body.model = router_result["model"]
    router_meta = router_result["meta"]
    router_ms = round((time.time() - router_start) * 1000, 2)
    if router_meta:
        plugin_trace.append({"plugin_name": "model_router", "duration_ms": router_ms, "status": "success", **router_meta})

    # 生成缓存键 (body.model is now resolved, body.messages are now sanitized)
    normalized_messages = json.dumps(body.messages, sort_keys=True, ensure_ascii=False)
    cache_key = cache_manager.generate_cache_key(
        normalized_prompt=normalized_messages,
        model=body.model,
        temperature=body.temperature or 1.0,
        max_tokens=body.max_tokens or 0,
        top_p=body.top_p or 1.0,
        user_id=user_id or "",
    )

    # 检查缓存（L1 -> L2 -> L3）
    cache_start = time.time()
    # 计算 embedding 向量用于 L3 语义缓存查询
    l3_vector = None
    if cache_manager._qdrant_client is not None:
        l3_vector = await _compute_l3_vector(normalized_messages)

    cache_kwargs: Dict[str, Any] = {"user_id": user_id}
    if l3_vector is not None:
        cache_kwargs["vector"] = l3_vector
    cached = await cache_manager.get(cache_key, value_fn=None, **cache_kwargs)
    cache_hit = cached is not None and cached.get("hit_tier") in ("L1", "L2", "L3")
    cache_ms = round((time.time() - cache_start) * 1000, 2)
    hit_tier_name = cached.get("hit_tier", "") if cached else ""
    cache_plugin_name = "semantic_cache" if hit_tier_name == "L3" else "prompt_cache"
    plugin_trace.append({"plugin_name": cache_plugin_name, "duration_ms": cache_ms, "status": "success"})

    if cache_hit and cached:
        # 缓存命中 -- 返回缓存的完整响应
        response_data = json.loads(cached["value"])
        hit_tier = cached.get("hit_tier", "L1")
        if metrics_collector:
            metrics_collector.inc_cache_hits(tier=hit_tier)
            # 记录缓存命中节省的 token
            cached_usage = response_data.get("usage", {})
            saved_tokens = cached_usage.get("total_tokens", 0)
            if saved_tokens > 0:
                metrics_collector.record_tokens_saved(saved_tokens)

        # 构建 _meta 元数据（API_CONTRACT.md 要求放在顶层）
        meta = {
            "cache_hit": True,
            "cache_tier": hit_tier,
            "routed_to": {
                "provider": "cache",
                "model": body.model,
                "tier": hit_tier,
            },
        }
        if pii_meta:
            meta["pii_detector"] = pii_meta
        if router_meta:
            meta["model_router"] = router_meta

        # 记录缓存命中日志
        cache_duration_ms = round((time.time() - request_start_time) * 1000, 1)
        # 记录 Prometheus 请求计数和延迟
        if metrics_collector:
            metrics_collector.record_request("POST", "/v1/chat/completions", "200")
            metrics_collector.record_duration("/v1/chat/completions", (time.time() - request_start_time))
        request.state.plugin_trace = plugin_trace
        await _record_request_log(
            request=request,
            method="POST", endpoint="/v1/chat/completions",
            status_code=200, duration_ms=cache_duration_ms, model=body.model,
            cache_hit=True, cache_tier=hit_tier,
        )

        return JSONResponse(content={
            "data": response_data,
            "message": "success",
            "_meta": meta,
        })

    # ===== 配额检查（缓存未命中时才检查，避免浪费） =====
    if metrics_collector:
        metrics_collector.inc_cache_misses()
    quota_start = time.time()
    if key_hash and key_store:
        # 预估 token 消耗（基于输入消息大小）
        estimated_tokens = sum(len(json.dumps(m)) for m in body.messages) // 4
        allowed, fail_msg, retry_after = await key_store.check_quota(
            key_hash=key_hash, tokens=estimated_tokens, cost=0.0
        )
        if not allowed:
            quota_ms = round((time.time() - quota_start) * 1000, 2)
            plugin_trace.append({"plugin_name": "quota_check", "duration_ms": quota_ms, "status": "failed"})
            headers = {}
            if retry_after > 0:
                headers["Retry-After"] = str(retry_after)
            # 映射失败原因到具体 error.code
            code = "quota_exceeded"
            if "RPM" in fail_msg:
                code = "rate_limit_rpm"
            elif "TPM" in fail_msg:
                code = "rate_limit_tpm"
            elif "Daily" in fail_msg:
                code = "quota_exceeded_daily_tokens"
            elif "Monthly" in fail_msg:
                code = "quota_exceeded_monthly_cost"
            request.state.plugin_trace = plugin_trace
            await _record_request_log(
                request=request,
                method="POST", endpoint="/v1/chat/completions",
                status_code=429, duration_ms=0, model=body.model,
                cache_hit=False, cache_tier=None,
            )
            return JSONResponse(
                content={"error": {"code": code, "message": fail_msg}},
                status_code=429,
                headers=headers,
            )
    quota_ms = round((time.time() - quota_start) * 1000, 2)
    plugin_trace.append({"plugin_name": "quota_check", "duration_ms": quota_ms, "status": "success"})

    # ===== Prompt Compression =====
    compress_start = time.time()
    compress_result = await _apply_prompt_compression(body, state)
    body.messages = compress_result["messages"]
    compress_meta = compress_result["meta"]
    compress_ms = round((time.time() - compress_start) * 1000, 2)
    if compress_meta and compress_meta.get("compression_ratio", 1.0) < 1.0:
        plugin_trace.append({"plugin_name": "prompt_compress", "duration_ms": compress_ms, "status": "success", **compress_meta})
    else:
        plugin_trace.append({"plugin_name": "prompt_compress", "duration_ms": compress_ms, "status": "skipped"})

    # 缓存未命中 -- 调用下游 LLM
    if litellm_bridge is None:
        return JSONResponse(
            content={"error": {"code": "internal_error", "message": "LiteLLM bridge not initialized"}},
            status_code=500,
        )

    # 用 RequestTracker 包裹：自动记录 active 请求数、请求总数、持续时间
    tracker = metrics_collector.track_request("/v1/chat/completions", method="POST") if metrics_collector else None
    if tracker:
        tracker.__enter__()

    llm_start = time.time()
    try:
        result = await litellm_bridge.completion(
            messages=body.messages,
            model=body.model,
            user_id=user_id,
            temperature=body.temperature,
            max_tokens=body.max_tokens,
            top_p=body.top_p,
            frequency_penalty=body.frequency_penalty,
            presence_penalty=body.presence_penalty,
            stream=False,
            tools=body.tools,
            tool_choice=body.tool_choice,
            stop=body.stop,
        )
    except Exception as exc:
        llm_ms = round((time.time() - llm_start) * 1000, 2)
        plugin_trace.append({"plugin_name": "llm_completion", "duration_ms": llm_ms, "status": "failed"})
        logger.error("LLM completion failed: %s", exc, exc_info=True)
        if tracker:
            tracker.__exit__(type(exc), exc, exc.__traceback__)
        return JSONResponse(
            content={"error": {"code": "internal_error", "message": f"Upstream completion error: {exc}"}},
            status_code=500,
        )

    llm_ms = round((time.time() - llm_start) * 1000, 2)
    plugin_trace.append({"plugin_name": "llm_completion", "duration_ms": llm_ms, "status": "success"})

    # 检查 bridge 返回的错误（如模型未注册）
    if "error" in result and "data" not in result:
        if tracker:
            tracker.__exit__(None, None, None)
        error_info = result["error"]
        error_code = error_info.get("code", "internal_error")
        status_code = 404 if error_code == "model_not_found" else 502
        request.state.plugin_trace = plugin_trace
        await _record_request_log(
            request=request,
            method="POST", endpoint="/v1/chat/completions",
            status_code=status_code, duration_ms=round((time.time() - request_start_time) * 1000, 1),
            model=body.model, cache_hit=False, cache_tier=None,
        )
        return JSONResponse(
            content={"error": error_info},
            status_code=status_code,
        )

    # 记录用量（Redis + Prometheus 指标）
    data_part = result.get("data", {})
    usage = data_part.get("usage", {})
    cost = result.get("_meta", {}).get("cost", 0.0)
    tokens_total = usage.get("total_tokens", 0)

    if key_hash:
        await key_store.increment_usage(
            key_hash=key_hash,
            tokens=tokens_total,
            cost=cost,
            model=body.model,
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
        )

    # Prometheus 指标
    if metrics_collector:
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        tt = usage.get("total_tokens", 0)
        if pt > 0:
            metrics_collector.record_tokens(pt, "prompt")
        if ct > 0:
            metrics_collector.record_tokens(ct, "completion")
        if tt > 0:
            # 优先使用 LiteLLM 返回的真实成本，否则 fallback 到估算
            final_cost = cost if cost > 0 else _estimate_cost(body.model, tt)
            if final_cost > 0:
                metrics_collector.record_cost(final_cost, model=body.model, user_id=user_id or "")
    # tracker.__exit__ 必须在 metrics_collector 块之外，确保无论如何都被调用
    if tracker:
        tracker.__exit__(None, None, None)

    # 回填缓存（L1 + L2 + 异步 L3）
    value_str = json.dumps(result.get("data", {}))
    cache_manager.l1_set(cache_key, value_str)
    # 同时回填 L2 Redis 缓存
    try:
        await cache_manager.l2_set(cache_key, value_str)
    except Exception as exc:
        logger.warning("L2 cache backfill failed: %s", exc)

    # 异步回填 L3 语义缓存（不阻塞响应）
    import asyncio
    asyncio.create_task(_safe_l3_backfill(
        cache_manager=cache_manager,
        cache_key=cache_key,
        value_str=value_str,
        normalized_messages=normalized_messages,
        model=body.model,
        user_id=user_id or "",
        token_count=usage.get("total_tokens", 0),
    ))

    # 记录请求日志到 Redis
    total_duration_ms = round((time.time() - request_start_time) * 1000, 1)
    # 将 plugin_trace 存储到 request.state 供 _record_request_log 使用
    request.state.plugin_trace = plugin_trace
    await _record_request_log(
        request=request,
        method="POST", endpoint="/v1/chat/completions",
        status_code=200, duration_ms=total_duration_ms, model=body.model,
        cache_hit=cache_hit, cache_tier=cached.get("hit_tier") if cached else None,
    )

    # Build enriched _meta
    enriched_meta = {
        **result.get("_meta", {"cache_hit": False, "cache_tier": None}),
        "media_optimization": mol_meta,
    }
    if pii_meta:
        enriched_meta["pii_detector"] = pii_meta
    if router_meta:
        enriched_meta["model_router"] = router_meta
    if compress_meta:
        enriched_meta["prompt_compress"] = compress_meta

    return JSONResponse(content={
        "data": result.get("data", {}),
        "message": "success",
        "_meta": enriched_meta,
    })


# ------------------------------------------------------------------
# POST /v1/chat/completions -- 流式
# ------------------------------------------------------------------


async def chat_completions_stream(
    body: ChatCompletionRequest,
    request: Request,
) -> Any:
    """流式聊天补全响应（SSE）。

    API_CONTRACT.md F15: 缓存命中时，将缓存的完整响应按 chunk 分块，
    以 20ms/chunk 延迟模拟真实 LLM 生成。
    """
    request_start_time = time.time()
    plugin_trace: list = []  # 手动记录各步骤耗时
    from .streaming import SSEGenerator, create_sse_response, simulate_stream_from_cache
    state = _get_app_state()
    cache_manager = state["cache_manager"]
    key_store = state["key_store"]
    litellm_bridge = state.get("litellm_bridge")
    metrics_collector = state["metrics_collector"]

    # 解析 user_id / key_hash
    user_id: Optional[str] = None
    key_hash: Optional[str] = None
    if hasattr(request.state, "api_key_data"):
        key_data = request.state.api_key_data
        if key_data:
            user_id = key_data.get("user_id") or None
            raw_key = getattr(request.state, "api_key_value", "")
            if raw_key:
                key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
    if not user_id and hasattr(request.state, "user_id"):
        user_id = request.state.user_id

    # ===== Media Optimization (V2) — 处理多模态内容 =====
    mol_start = time.time()
    mol_result = await _apply_media_optimization(body, request, state)
    body.messages = mol_result["messages"]
    mol_meta = mol_result["meta"]
    mol_ms = round((time.time() - mol_start) * 1000, 2)
    if mol_meta:
        plugin_trace.append({"plugin_name": "media_optimization", "duration_ms": mol_ms, "status": "success"})

    # ===== PII Detection =====
    pii_start = time.time()
    pii_result = await _apply_pii_detection(body, request, state)
    if "error" in pii_result:
        pii_ms = round((time.time() - pii_start) * 1000, 2)
        plugin_trace.append({"plugin_name": "pii_detector", "duration_ms": pii_ms, "status": "rejected"})
        request.state.plugin_trace = plugin_trace
        await _record_request_log(
            request=request, method="POST", endpoint="/v1/chat/completions",
            status_code=403, duration_ms=0, model=body.model,
            cache_hit=False, cache_tier=None,
        )
        return JSONResponse(
            content=pii_result["error"],
            status_code=403,
        )
    body.messages = pii_result["messages"]
    pii_meta = pii_result["meta"]
    pii_ms = round((time.time() - pii_start) * 1000, 2)
    if pii_meta:
        plugin_trace.append({"plugin_name": "pii_detector", "duration_ms": pii_ms, "status": "success", **pii_meta})

    # ===== Model Router (auto) =====
    router_start = time.time()
    router_result = await _resolve_auto_model(body, state)
    if "error" in router_result:
        router_ms = round((time.time() - router_start) * 1000, 2)
        plugin_trace.append({"plugin_name": "model_router", "duration_ms": router_ms, "status": "failed"})
        request.state.plugin_trace = plugin_trace
        await _record_request_log(
            request=request, method="POST", endpoint="/v1/chat/completions",
            status_code=400, duration_ms=0, model=body.model,
            cache_hit=False, cache_tier=None,
        )
        return JSONResponse(
            content=router_result["error"],
            status_code=400,
        )
    body.model = router_result["model"]
    router_meta = router_result["meta"]
    router_ms = round((time.time() - router_start) * 1000, 2)
    if router_meta:
        plugin_trace.append({"plugin_name": "model_router", "duration_ms": router_ms, "status": "success", **router_meta})

    # 生成缓存键并检查缓存
    cache_start = time.time()
    normalized_messages = json.dumps(body.messages, sort_keys=True, ensure_ascii=False)
    cache_key = cache_manager.generate_cache_key(
        normalized_prompt=normalized_messages,
        model=body.model,
        temperature=body.temperature or 1.0,
        max_tokens=body.max_tokens or 0,
        top_p=body.top_p or 1.0,
        user_id=user_id or "",
    )

    # 计算 embedding 向量用于 L3 语义缓存查询
    l3_vector = None
    if cache_manager._qdrant_client is not None:
        l3_vector = await _compute_l3_vector(normalized_messages)

    cache_kwargs: Dict[str, Any] = {"user_id": user_id}
    if l3_vector is not None:
        cache_kwargs["vector"] = l3_vector
    cached = await cache_manager.get(cache_key, value_fn=None, **cache_kwargs)
    if cached is not None and cached.get("hit_tier") in ("L1", "L2", "L3"):
        # 缓存命中 — 模拟流式响应（F15）
        cache_ms = round((time.time() - cache_start) * 1000, 2)
        hit_tier = cached.get("hit_tier", "L1")
        cache_plugin_name = "semantic_cache" if hit_tier == "L3" else "prompt_cache"
        plugin_trace.append({"plugin_name": cache_plugin_name, "duration_ms": cache_ms, "status": "success"})
        if metrics_collector:
            metrics_collector.inc_cache_hits(tier=hit_tier)
            # 记录缓存命中节省的 token
            try:
                cached_data = json.loads(cached["value"])
                saved_tokens = cached_data.get("usage", {}).get("total_tokens", 0)
                if saved_tokens > 0:
                    metrics_collector.record_tokens_saved(saved_tokens)
            except (json.JSONDecodeError, AttributeError):
                pass
        response_json = cached["value"]

        chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        stream_gen = simulate_stream_from_cache(response_json, hit_tier=hit_tier)

        # 记录缓存命中日志（流式）
        if metrics_collector:
            metrics_collector.record_request("POST", "/v1/chat/completions", "200")
            metrics_collector.record_duration("/v1/chat/completions", 0.001)  # 缓存命中极低延迟
        request.state.plugin_trace = plugin_trace
        await _record_request_log(
            request=request,
            method="POST", endpoint="/v1/chat/completions",
            status_code=200, duration_ms=0, model=body.model,
            cache_hit=True, cache_tier=hit_tier,
        )
        return create_sse_response(stream_gen, chat_id=chat_id)

    cache_ms = round((time.time() - cache_start) * 1000, 2)
    plugin_trace.append({"plugin_name": "prompt_cache", "duration_ms": cache_ms, "status": "success"})

    # ===== 配额检查 =====
    if metrics_collector:
        metrics_collector.inc_cache_misses()
    quota_start = time.time()
    if key_hash and key_store:
        estimated_tokens = sum(len(json.dumps(m)) for m in body.messages) // 4
        allowed, fail_msg, retry_after = await key_store.check_quota(
            key_hash=key_hash, tokens=estimated_tokens, cost=0.0
        )
        if not allowed:
            quota_ms = round((time.time() - quota_start) * 1000, 2)
            plugin_trace.append({"plugin_name": "quota_check", "duration_ms": quota_ms, "status": "failed"})
            headers = {}
            if retry_after > 0:
                headers["Retry-After"] = str(retry_after)
            code = "quota_exceeded"
            if "RPM" in fail_msg:
                code = "rate_limit_rpm"
            elif "TPM" in fail_msg:
                code = "rate_limit_tpm"
            elif "Daily" in fail_msg:
                code = "quota_exceeded_daily_tokens"
            elif "Monthly" in fail_msg:
                code = "quota_exceeded_monthly_cost"
            request.state.plugin_trace = plugin_trace
            await _record_request_log(
                request=request,
                method="POST", endpoint="/v1/chat/completions",
                status_code=429, duration_ms=0, model=body.model,
                cache_hit=False, cache_tier=None,
            )
            return JSONResponse(
                content={"error": {"code": code, "message": fail_msg}},
                status_code=429,
                headers=headers,
            )
    quota_ms = round((time.time() - quota_start) * 1000, 2)
    plugin_trace.append({"plugin_name": "quota_check", "duration_ms": quota_ms, "status": "success"})

    # ===== Prompt Compression =====
    compress_start = time.time()
    compress_result = await _apply_prompt_compression(body, state)
    body.messages = compress_result["messages"]
    compress_meta = compress_result["meta"]
    compress_ms = round((time.time() - compress_start) * 1000, 2)
    if compress_meta and compress_meta.get("compression_ratio", 1.0) < 1.0:
        plugin_trace.append({"plugin_name": "prompt_compress", "duration_ms": compress_ms, "status": "success", **compress_meta})
    else:
        plugin_trace.append({"plugin_name": "prompt_compress", "duration_ms": compress_ms, "status": "skipped"})

    if litellm_bridge is None:
        return JSONResponse(
            content={"error": {"code": "internal_error", "message": "LiteLLM bridge not initialized"}},
            status_code=500,
        )

    # 调用下游 LLM 流式
    plugin_trace.append({"plugin_name": "llm_completion", "duration_ms": 0, "status": "success"})
    request.state.plugin_trace = plugin_trace
    completion_gen = litellm_bridge.completion_stream(
        messages=body.messages,
        model=body.model,
        user_id=user_id,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        top_p=body.top_p,
        frequency_penalty=body.frequency_penalty,
        presence_penalty=body.presence_penalty,
        tools=body.tools,
        tool_choice=body.tool_choice,
        stop=body.stop,
    )

    # 包装生成器：消费完所有 chunk 后从最后一个提取 usage 并记录指标
    if metrics_collector:
        # 记录请求计数和延迟（流式请求在开始时就计数，延迟为到首字节时间）
        stream_start_duration = time.time() - request_start_time
        metrics_collector.record_request("POST", "/v1/chat/completions", "200")
        metrics_collector.record_duration("/v1/chat/completions", stream_start_duration)
        completion_gen = _wrap_stream_for_metrics(completion_gen, metrics_collector, body.model, user_id=user_id or "")

    return create_sse_response(completion_gen, chat_id=f"chatcmpl-{uuid.uuid4().hex[:12]}")


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
        "data": {"object": "list", "data": models},
        "message": "success",
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
        if body.stream:
            return await chat_completions_stream(body, request)
        return await chat_completions_non_stream(body, request)

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
