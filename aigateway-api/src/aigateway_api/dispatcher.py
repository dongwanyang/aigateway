"""RequestDispatcher — 总分总架构的「总入口」。

职责:
1. 分流:把每个 /v1/chat/completions 请求分类为 understanding | generation
2. 分发:调对应管道的 PipelineEngine 跑插件链
3. 编排:插件链跑完后,统一处理缓存/配额/LiteLLM 出口/回填/短路返回

LiteLLMBridge 是两条管道的统一出口（「总出口」）。

分流依据（优先级序）:
1. 模型名推断:body.model 命中 providers 里标记 generative 模态的模型 → generation
2. 模态推断:messages 含 image/audio/video content → 倾向 generation
3. 默认:understanding
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from aigateway_core.context import PipelineContext

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 分流器
# ------------------------------------------------------------------


def _has_multimodal_content(messages: list) -> bool:
    """messages 是否含 list 类型 content（多模态：图片/音频/视频）。"""
    for m in messages or []:
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            return True
    return False


def _content_modality_hint(messages: list) -> Optional[str]:
    """从多模态 content 推断模态倾向。

    返回 "generation" 表示含 image/audio/video 输入块（倾向生成/多模态理解），
    None 表示纯文本。
    """
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type", "")
                if btype in ("image_url", "input_image", "image"):
                    return "generation"
                if btype in ("input_audio", "audio"):
                    return "generation"
                if btype in ("video", "input_video"):
                    return "generation"
    return None


def classify_request(body: Any, config_manager: Any) -> str:
    """把请求分类为 understanding | generation。

    Args:
        body: ChatCompletionRequest（或 dict），需有 model 和 messages。
        config_manager: ConfigManager，用于查 providers 模型模态配置。

    Returns:
        "understanding" | "generation"
    """
    model = getattr(body, "model", None) or (body.get("model") if isinstance(body, dict) else None)
    messages = getattr(body, "messages", None) or (body.get("messages") if isinstance(body, dict) else None)

    # 1. 模型名推断：查 providers 里该模型是否标记为 generative 模态
    if model and model != "auto" and config_manager is not None:
        try:
            providers = config_manager.get("providers", {}) or {}
            for prov in providers.values():
                if not isinstance(prov, dict):
                    continue
                for group in prov.get("model_grouper", []) or []:
                    if not isinstance(group, dict):
                        continue
                    for m in group.get("models", []) or []:
                        if isinstance(m, dict) and m.get("name") == model:
                            modalities = m.get("modalities") or m.get("modality")
                            if modalities:
                                if isinstance(modalities, str):
                                    modalities = [modalities]
                                if "generative" in modalities or "image" in modalities or "video" in modalities:
                                    return "generation"
                            # 命中模型但无 generative 标记 → understanding
                            return "understanding"
        except Exception as exc:
            logger.debug("classify_request 模型推断异常: %s", exc)

    # 2. 模态推断：含多模态输入块倾向 generation
    if messages and _content_modality_hint(messages) == "generation":
        return "generation"

    # 3. 默认 understanding
    return "understanding"


# ------------------------------------------------------------------
# Dispatcher
# ------------------------------------------------------------------


class RequestDispatcher:
    """总分总架构的请求分发器。

    持有两条管道的 PipelineEngine，按分流结果把请求交给对应管道，
    插件链跑完后统一编排缓存/配额/LiteLLM 出口/回填。
    """

    def __init__(self, state: Dict[str, Any]) -> None:
        self.state = state
        self.understanding_engine = state.get("understanding_engine")
        self.generation_engine = state.get("generation_engine")
        self.cache_manager = state.get("cache_manager")
        self.key_store = state.get("key_store")
        self.litellm_bridge = state.get("litellm_bridge")
        self.metrics_collector = state.get("metrics_collector")
        self.config_manager = state.get("config_manager")

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------

    async def dispatch(self, body: Any, request: Request) -> JSONResponse:
        """分发请求到对应管道。

        body.should_stream 决定走流式还是非流式编排。
        """
        # 解析 user_id / key_hash（从鉴权中间件注入）
        user_id, key_hash = self._resolve_identity(request)

        # 分流
        pipeline_kind = classify_request(body, self.config_manager)
        logger.info(
            "dispatch: pipeline_kind=%s, model=%s, stream=%s",
            pipeline_kind,
            getattr(body, "model", None),
            getattr(body, "stream", False),
        )

        engine = self.understanding_engine if pipeline_kind == "understanding" else self.generation_engine

        if pipeline_kind == "understanding":
            return await self._dispatch_understanding(body, request, engine, user_id, key_hash)
        return await self._dispatch_generation(body, request, engine, user_id, key_hash)

    # ------------------------------------------------------------------
    # 身份解析
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_identity(request: Request) -> tuple:
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
        return user_id, key_hash

    # ------------------------------------------------------------------
    # 理解管道编排
    # ------------------------------------------------------------------

    async def _dispatch_understanding(
        self, body: Any, request: Request, engine: Any, user_id: Optional[str], key_hash: Optional[str]
    ) -> JSONResponse:
        """理解管道：engine 插件链 → 缓存 → 配额 → LiteLLM → 回填。

        复用 openai_compat 的辅助函数（_apply_media_optimization 等），
        dispatcher 只负责编排顺序和短路返回。
        """
        from aigateway_api.openai_compat import (
            _apply_media_optimization,
            _apply_pii_detection,
            _apply_prompt_compression,
            _record_request_log,
            _resolve_auto_model,
        )
        from aigateway_api.streaming import create_sse_response, simulate_stream_from_cache

        request_start_time = time.time()
        plugin_trace: list = []
        state = self.state

        # ===== 前置共享处理（两管道共用，分流后仍跑一遍）=====
        # Media Optimization
        mol_start = time.time()
        mol_result = await _apply_media_optimization(body, request, state)
        body.messages = mol_result["messages"]
        mol_meta = mol_result["meta"]
        if mol_meta:
            plugin_trace.append({"plugin_name": "media_optimization",
                                 "duration_ms": round((time.time() - mol_start) * 1000, 2),
                                 "status": "success"})

        # PII Detection
        pii_start = time.time()
        pii_result = await _apply_pii_detection(body, request, state)
        if "error" in pii_result:
            plugin_trace.append({"plugin_name": "pii_detector",
                                 "duration_ms": round((time.time() - pii_start) * 1000, 2),
                                 "status": "rejected"})
            request.state.plugin_trace = plugin_trace
            await _record_request_log(request=request, method="POST", endpoint="/v1/chat/completions",
                                      status_code=403, duration_ms=0, model=body.model,
                                      cache_hit=False, cache_tier=None)
            return JSONResponse(content=pii_result["error"], status_code=403)
        body.messages = pii_result["messages"]
        pii_meta = pii_result["meta"]
        if pii_meta:
            plugin_trace.append({"plugin_name": "pii_detector",
                                 "duration_ms": round((time.time() - pii_start) * 1000, 2),
                                 "status": "success", **pii_meta})

        # Model Router (auto)
        router_start = time.time()
        router_result = await _resolve_auto_model(body, state)
        if "error" in router_result:
            plugin_trace.append({"plugin_name": "model_router",
                                 "duration_ms": round((time.time() - router_start) * 1000, 2),
                                 "status": "failed"})
            request.state.plugin_trace = plugin_trace
            await _record_request_log(request=request, method="POST", endpoint="/v1/chat/completions",
                                      status_code=400, duration_ms=0, model=body.model,
                                      cache_hit=False, cache_tier=None)
            return JSONResponse(content=router_result["error"], status_code=400)
        body.model = router_result["model"]
        router_meta = router_result["meta"]
        if router_meta:
            plugin_trace.append({"plugin_name": "model_router",
                                 "duration_ms": round((time.time() - router_start) * 1000, 2),
                                 "status": "success", **router_meta})

        # ===== 跑理解管道 engine 插件链（rag_retriever / conv_compressor 等）=====
        # 注意：pii/cache/semantic/model_router/compress 已由上面的辅助函数处理
        # （它们各自建独立 ctx 调插件），engine 这里跑的是注册到 understanding 且
        # 未被上述辅助函数覆盖的插件。为避免重复执行，engine 跑前先过滤掉已处理的。
        if engine is not None:
            try:
                ctx = PipelineContext(
                    request={"messages": body.messages, "model": body.model, "stream": getattr(body, "stream", False)},
                    pipeline_kind="understanding",
                    user_id=user_id,
                )
                ctx.should_stream = getattr(body, "stream", False)
                # 过滤掉已被辅助函数处理的核心插件，避免重复执行
                ctx._skip_names = {"pii_detector", "prompt_cache", "semantic_cache",
                                   "model_router", "prompt_compress", "media_optimizer"}
                ctx = await self._run_engine_filtered(engine, ctx)
            except Exception as exc:
                logger.warning("理解管道 engine 执行异常（fail-open 继续）: %s", exc)

        # ===== 缓存查找 =====
        cache_manager = self.cache_manager
        normalized_messages = json.dumps(body.messages, sort_keys=True, ensure_ascii=False)
        cache_key = cache_manager.generate_cache_key(
            normalized_prompt=normalized_messages,
            model=body.model,
            temperature=body.temperature or 1.0,
            max_tokens=body.max_tokens or 0,
            top_p=body.top_p or 1.0,
            user_id=user_id or "",
        )

        cache_start = time.time()
        cache_kwargs: Dict[str, Any] = {"user_id": user_id}
        if cache_manager._qdrant_client is not None:
            from aigateway_api.openai_compat import _compute_l3_vector
            l3_vec = await _compute_l3_vector(normalized_messages)
            if l3_vec is not None:
                cache_kwargs["vector"] = l3_vec

        cached = await cache_manager.get(cache_key, value_fn=None, **cache_kwargs)
        hit_tier = cached.get("hit_tier") if cached else None
        cache_hit = cached is not None and hit_tier in ("L1", "L2", "L3")
        cache_plugin_name = "semantic_cache" if hit_tier == "L3" else "prompt_cache"
        plugin_trace.append({"plugin_name": cache_plugin_name,
                             "duration_ms": round((time.time() - cache_start) * 1000, 2),
                             "status": "success"})

        # 缓存命中短路
        if cache_hit:
            return await self._handle_cache_hit(
                body, request, cached, hit_tier, plugin_trace, request_start_time,
                pii_meta, router_meta, mol_meta, user_id,
            )

        # ===== 配额检查 =====
        key_store = self.key_store
        quota_start = time.time()
        if key_hash and key_store:
            estimated_tokens = sum(len(json.dumps(m)) for m in body.messages) // 4
            allowed, fail_msg, retry_after = await key_store.check_quota(
                key_hash=key_hash, tokens=estimated_tokens, cost=0.0
            )
            if not allowed:
                plugin_trace.append({"plugin_name": "quota_check",
                                     "duration_ms": round((time.time() - quota_start) * 1000, 2),
                                     "status": "failed"})
                request.state.plugin_trace = plugin_trace
                headers = {}
                if retry_after and retry_after > 0:
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
                await _record_request_log(request=request, method="POST", endpoint="/v1/chat/completions",
                                          status_code=429, duration_ms=0, model=body.model,
                                          cache_hit=False, cache_tier=None)
                return JSONResponse(content={"error": {"code": code, "message": fail_msg}},
                                   status_code=429, headers=headers)
        plugin_trace.append({"plugin_name": "quota_check",
                             "duration_ms": round((time.time() - quota_start) * 1000, 2),
                             "status": "success"})

        # ===== Prompt Compression =====
        compress_start = time.time()
        compress_result = await _apply_prompt_compression(body, state)
        body.messages = compress_result["messages"]
        compress_meta = compress_result["meta"]
        if compress_meta and compress_meta.get("compression_ratio", 1.0) < 1.0:
            plugin_trace.append({"plugin_name": "prompt_compress",
                                 "duration_ms": round((time.time() - compress_start) * 1000, 2),
                                 "status": "success", **compress_meta})
        else:
            plugin_trace.append({"plugin_name": "prompt_compress",
                                 "duration_ms": round((time.time() - compress_start) * 1000, 2),
                                 "status": "skipped"})

        # ===== LiteLLM 出口 =====
        litellm_bridge = self.litellm_bridge
        if litellm_bridge is None:
            return JSONResponse(
                content={"error": {"code": "internal_error", "message": "LiteLLM bridge not initialized"}},
                status_code=500,
            )

        if getattr(body, "stream", False):
            return await self._call_llm_stream(
                body, request, litellm_bridge, plugin_trace, request_start_time,
                user_id, key_hash, cache_key,
            )
        return await self._call_llm_nonstream(
            body, request, litellm_bridge, plugin_trace, request_start_time,
            user_id, key_hash, cache_key, pii_meta, router_meta, mol_meta, compress_meta,
        )

    # ------------------------------------------------------------------
    # 生成管道编排
    # ------------------------------------------------------------------

    async def _dispatch_generation(
        self, body: Any, request: Request, engine: Any, user_id: Optional[str], key_hash: Optional[str]
    ) -> JSONResponse:
        """生成管道：engine 插件链 → 配额 → LiteLLM（不查理解缓存）。

        生成结果（图片/视频）缓存语义复杂，本管道默认不查 prompt_cache；
        可选走 MediaCacheManager（本次不接入，留待后续）。
        """
        from aigateway_api.openai_compat import _record_request_log

        request_start_time = time.time()
        plugin_trace: list = []
        state = self.state

        # 跑生成管道 engine 插件链（ai_director → ... → cost_tracker）
        if engine is not None:
            try:
                ctx = PipelineContext(
                    request={"messages": body.messages, "model": body.model,
                             "stream": getattr(body, "stream", False)},
                    pipeline_kind="generation",
                    user_id=user_id,
                )
                ctx.should_stream = getattr(body, "stream", False)
                ctx = await engine.execute_ctx(ctx)
                # 插件链可能改写 messages / model，回写
                req = ctx.request
                if isinstance(req, dict):
                    body.messages = req.get("messages", body.messages)
                    if req.get("model"):
                        body.model = req["model"]
            except Exception as exc:
                logger.warning("生成管道 engine 执行异常（fail-open 继续）: %s", exc)

        # 配额检查
        key_store = self.key_store
        quota_start = time.time()
        if key_hash and key_store:
            estimated_tokens = sum(len(json.dumps(m)) for m in body.messages) // 4
            allowed, fail_msg, retry_after = await key_store.check_quota(
                key_hash=key_hash, tokens=estimated_tokens, cost=0.0
            )
            if not allowed:
                plugin_trace.append({"plugin_name": "quota_check",
                                     "duration_ms": round((time.time() - quota_start) * 1000, 2),
                                     "status": "failed"})
                request.state.plugin_trace = plugin_trace
                headers = {}
                if retry_after and retry_after > 0:
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
                await _record_request_log(request=request, method="POST", endpoint="/v1/chat/completions",
                                          status_code=429, duration_ms=0, model=body.model,
                                          cache_hit=False, cache_tier=None)
                return JSONResponse(content={"error": {"code": code, "message": fail_msg}},
                                   status_code=429, headers=headers)
        plugin_trace.append({"plugin_name": "quota_check",
                             "duration_ms": round((time.time() - quota_start) * 1000, 2),
                             "status": "success"})

        # ===== LiteLLM 出口（生成管道直接调，不查缓存）=====
        litellm_bridge = self.litellm_bridge
        if litellm_bridge is None:
            return JSONResponse(
                content={"error": {"code": "internal_error", "message": "LiteLLM bridge not initialized"}},
                status_code=500,
            )

        if getattr(body, "stream", False):
            return await self._call_llm_stream(
                body, request, litellm_bridge, plugin_trace, request_start_time,
                user_id, key_hash, cache_key=None,  # 生成管道不回填
            )
        return await self._call_llm_nonstream(
            body, request, litellm_bridge, plugin_trace, request_start_time,
            user_id, key_hash, cache_key=None,  # 生成管道不回填
            pii_meta=None, router_meta=None, mol_meta=None, compress_meta=None,
        )

    # ------------------------------------------------------------------
    # LiteLLM 出口（两条管道共用）
    # ------------------------------------------------------------------

    async def _call_llm_nonstream(
        self, body, request, litellm_bridge, plugin_trace, request_start_time,
        user_id, key_hash, cache_key,
        pii_meta=None, router_meta=None, mol_meta=None, compress_meta=None,
    ) -> JSONResponse:
        """非流式调 LiteLLM 出口 + 用量记录 + 缓存回填。"""
        from aigateway_api.openai_compat import _estimate_cost, _record_request_log

        metrics_collector = self.metrics_collector
        cache_manager = self.cache_manager
        key_store = self.key_store

        tracker = None
        if metrics_collector:
            tracker = metrics_collector.track_request("/v1/chat/completions", method="POST")
            tracker.__enter__()

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
                tools=body.tools,
                tool_choice=body.tool_choice,
                stop=body.stop,
            )
        except Exception as exc:
            plugin_trace.append({"plugin_name": "llm_completion",
                                 "duration_ms": round((time.time() - request_start_time) * 1000, 2),
                                 "status": "failed"})
            logger.error("LLM completion failed: %s", exc, exc_info=True)
            if tracker:
                tracker.__exit__(type(exc), exc, exc.__traceback__)
            return JSONResponse(
                content={"error": {"code": "internal_error",
                                   "message": f"Upstream completion error: {exc}"}},
                status_code=500,
            )

        plugin_trace.append({"plugin_name": "llm_completion",
                             "duration_ms": round((time.time() - request_start_time) * 1000, 2),
                             "status": "success"})

        # bridge 返回错误
        if "error" in result and "data" not in result:
            if tracker:
                tracker.__exit__(None, None, None)
            error_info = result.get("error", {})
            error_code = error_info.get("code", "internal_error") if isinstance(error_info, dict) else "internal_error"
            status_code = 404 if error_code == "model_not_found" else 502
            request.state.plugin_trace = plugin_trace
            await _record_request_log(request=request, method="POST", endpoint="/v1/chat/completions",
                                      status_code=status_code,
                                      duration_ms=round((time.time() - request_start_time) * 1000, 1),
                                      model=body.model, cache_hit=False, cache_tier=None)
            return JSONResponse(content={"error": error_info}, status_code=status_code)

        if tracker:
            tracker.__exit__(None, None, None)

        # usage / cost 记录
        usage = result.get("usage", {}) or result.get("data", {}).get("usage", {})
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        tt = usage.get("total_tokens", 0)
        cost = result.get("_meta", {}).get("cost", 0.0)

        if key_hash and key_store:
            try:
                await key_store.increment_usage(
                    key_hash, tokens=tt, cost=cost, model=body.model,
                    tokens_in=pt, tokens_out=ct,
                )
            except Exception as exc:
                logger.warning("increment_usage 失败: %s", exc)

        if metrics_collector:
            if pt > 0:
                metrics_collector.record_tokens(pt, "prompt")
            if ct > 0:
                metrics_collector.record_tokens(ct, "completion")
            final_cost = cost if cost > 0 else _estimate_cost(body.model, tt)
            if tt > 0 and final_cost > 0:
                metrics_collector.record_cost(final_cost, model=body.model, user_id=user_id or "")

        # 缓存回填（生成管道 cache_key=None 不回填）
        if cache_key and cache_manager:
            try:
                value_str = json.dumps(result.get("data", {}))
                cache_manager.l1_set(cache_key, value_str)
                try:
                    await cache_manager.l2_set(cache_key, value_str)
                except Exception as exc:
                    logger.warning("L2 回填失败: %s", exc)
                # L3 异步回填
                import asyncio
                from aigateway_api.openai_compat import _safe_l3_backfill
                asyncio.create_task(_safe_l3_backfill(
                    cache_manager, cache_key, value_str,
                    normalized_messages, body.model, user_id or "", tt,
                ))
            except Exception as exc:
                logger.warning("缓存回填失败: %s", exc)

        total_duration_ms = round((time.time() - request_start_time) * 1000, 1)
        request.state.plugin_trace = plugin_trace
        await _record_request_log(request=request, method="POST", endpoint="/v1/chat/completions",
                                  status_code=200, duration_ms=total_duration_ms,
                                  model=body.model, cache_hit=False, cache_tier=None)

        meta = dict(result.get("_meta", {}))
        meta.update({
            "media_optimization": mol_meta,
            "pii_detector": pii_meta,
            "model_router": router_meta,
            "prompt_compress": compress_meta,
        })
        meta = {k: v for k, v in meta.items() if v}
        return JSONResponse(
            content={"data": result.get("data", {}), "message": "success", "_meta": meta},
            status_code=200,
        )

    async def _call_llm_stream(
        self, body, request, litellm_bridge, plugin_trace, request_start_time,
        user_id, key_hash, cache_key,
    ) -> JSONResponse:
        """流式调 LiteLLM 出口。

        流式修正（对齐非流式）:
        - 扣配额:流结束后从 last_chunk.usage 取 token 调 increment_usage
        - 回填缓存:cache_key 非空时回填 L1/L2/L3
        - cost 真实值:优先用 bridge 返回,否则估算
        - llm_completion duration_ms 不写死 0
        """
        from aigateway_api.openai_compat import (
            _estimate_cost, _record_request_log,
        )
        from aigateway_api.streaming import create_sse_response

        metrics_collector = self.metrics_collector
        cache_manager = self.cache_manager
        key_store = self.key_store

        llm_start = time.time()
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

        # 包装生成器：消费完后做配额扣减 + 缓存回填 + metrics（修正后行为）
        completion_gen = self._wrap_stream_full(
            completion_gen, metrics_collector, cache_manager, key_store,
            body.model, user_id, key_hash, cache_key, llm_start,
        )

        plugin_trace.append({"plugin_name": "llm_completion",
                             "duration_ms": round((time.time() - llm_start) * 1000, 2),
                             "status": "success"})
        request.state.plugin_trace = plugin_trace

        if metrics_collector:
            stream_start_duration = time.time() - request_start_time
            metrics_collector.record_request("POST", "/v1/chat/completions", "200")
            metrics_collector.record_duration("/v1/chat/completions", stream_start_duration)

        await _record_request_log(request=request, method="POST", endpoint="/v1/chat/completions",
                                  status_code=200, duration_ms=0,
                                  model=body.model, cache_hit=False, cache_tier=None)
        return create_sse_response(completion_gen, chat_id=f"chatcmpl-{uuid.uuid4().hex[:12]}")

    async def _wrap_stream_full(
        self, gen, metrics_collector, cache_manager, key_store,
        model, user_id, key_hash, cache_key, llm_start,
    ):
        """流式包装器:透传 chunk + 末尾做配额/缓存/metrics。

        合并了原 _wrap_stream_for_metrics 的 metrics 逻辑，并新增:
        - increment_usage（原流式不扣，本次修正）
        - 缓存回填（原流式不回填，本次修正）
        """
        from aigateway_api.openai_compat import _estimate_cost

        last_chunk = {}
        collected = []
        async for chunk in gen:
            last_chunk = chunk
            collected.append(chunk)
            yield chunk

        usage = last_chunk.get("usage", {}) if isinstance(last_chunk, dict) else {}
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        tt = usage.get("total_tokens", 0)

        if not usage:
            return

        # metrics
        if metrics_collector:
            if pt > 0:
                metrics_collector.record_tokens(pt, "prompt")
            if ct > 0:
                metrics_collector.record_tokens(ct, "completion")
            final_cost = _estimate_cost(model, tt)
            if tt > 0 and final_cost > 0:
                metrics_collector.record_cost(final_cost, model=model, user_id=user_id or "")

        # 配额扣减（修正点：原流式不扣）
        if key_hash and key_store and tt > 0:
            try:
                await key_store.increment_usage(
                    key_hash, tokens=tt, cost=_estimate_cost(model, tt),
                    model=model, tokens_in=pt, tokens_out=ct,
                )
            except Exception as exc:
                logger.warning("流式 increment_usage 失败: %s", exc)

        # 缓存回填（修正点：原流式不回填）
        if cache_key and cache_manager and collected:
            try:
                # 拼一个非流式格式的 data 用于回填
                value_str = json.dumps({"choices": [], "usage": usage})
                cache_manager.l1_set(cache_key, value_str)
                try:
                    await cache_manager.l2_set(cache_key, value_str)
                except Exception as exc:
                    logger.warning("流式 L2 回填失败: %s", exc)
            except Exception as exc:
                logger.warning("流式缓存回填失败: %s", exc)

    # ------------------------------------------------------------------
    # 缓存命中处理
    # ------------------------------------------------------------------

    async def _handle_cache_hit(
        self, body, request, cached, hit_tier, plugin_trace, request_start_time,
        pii_meta, router_meta, mol_meta, user_id,
    ) -> JSONResponse:
        """缓存命中：非流式直接返回，流式走 simulate_stream_from_cache。"""
        from aigateway_api.openai_compat import _record_request_log
        from aigateway_api.streaming import create_sse_response, simulate_stream_from_cache

        metrics_collector = self.metrics_collector
        cache_duration_ms = round((time.time() - request_start_time) * 1000, 1)

        if getattr(body, "stream", False):
            chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
            stream_gen = simulate_stream_from_cache(cached["value"], hit_tier=hit_tier)
            if metrics_collector:
                metrics_collector.inc_cache_hits(tier=hit_tier)
                metrics_collector.record_request("POST", "/v1/chat/completions", "200")
                metrics_collector.record_duration("/v1/chat/completions", 0.001)
            try:
                resp_data = json.loads(cached["value"])
                saved = resp_data.get("usage", {}).get("total_tokens", 0)
                if saved > 0 and metrics_collector:
                    metrics_collector.record_tokens_saved(saved)
            except (json.JSONDecodeError, AttributeError):
                pass
            request.state.plugin_trace = plugin_trace
            await _record_request_log(request=request, method="POST", endpoint="/v1/chat/completions",
                                      status_code=200, duration_ms=0,
                                      model=body.model, cache_hit=True, cache_tier=hit_tier)
            return create_sse_response(stream_gen, chat_id=chat_id)

        # 非流式
        response_data = json.loads(cached["value"])
        if metrics_collector:
            metrics_collector.inc_cache_hits(tier=hit_tier)
            saved = response_data.get("usage", {}).get("total_tokens", 0)
            if saved > 0:
                metrics_collector.record_tokens_saved(saved)
            metrics_collector.record_request("POST", "/v1/chat/completions", "200")
            metrics_collector.record_duration("/v1/chat/completions", cache_duration_ms / 1000)

        request.state.plugin_trace = plugin_trace
        await _record_request_log(request=request, method="POST", endpoint="/v1/chat/completions",
                                  status_code=200, duration_ms=cache_duration_ms,
                                  model=body.model, cache_hit=True, cache_tier=hit_tier)

        meta = {
            "cache_hit": True,
            "cache_tier": hit_tier,
            "routed_to": {"provider": "cache", "model": body.model, "tier": hit_tier},
        }
        if pii_meta:
            meta["pii_detector"] = pii_meta
        if router_meta:
            meta["model_router"] = router_meta
        return JSONResponse(
            content={"data": response_data, "message": "success", "_meta": meta},
            status_code=200,
        )

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    async def _run_engine_filtered(self, engine: Any, ctx: PipelineContext) -> PipelineContext:
        """跑 engine 插件链，跳过 ctx._skip_names 里的插件（已被辅助函数处理）。

        Engine 本身没有 skip 机制，这里临时把 _ordered_plugins 里在 skip 集合的
        插件过滤掉再执行，避免与辅助函数重复执行 pii/cache 等。
        """
        import time as _time
        skip = getattr(ctx, "_skip_names", set())
        if not skip or engine is None or not getattr(engine, "_initialized", False):
            if engine is not None:
                return await engine.execute_ctx(ctx)
            return ctx

        pipeline_start = _time.monotonic()
        for plugin in engine._ordered_plugins:
            if ctx.should_stop:
                break
            if plugin.name in skip:
                continue
            pstart = _time.monotonic()
            try:
                ctx = await plugin.execute(ctx)
            except Exception as exc:
                elapsed = (_time.monotonic() - pstart) * 1000
                ctx.add_plugin_trace(plugin.name, elapsed, "failed")
                logger.warning("插件 %s 执行失败（fail-open）: %s", plugin.name, exc)
                continue
            elapsed = (_time.monotonic() - pstart) * 1000
            ctx.add_plugin_trace(plugin.name, elapsed, "success")
        return ctx
