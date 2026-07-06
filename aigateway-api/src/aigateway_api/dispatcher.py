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


def _emit_stage(trace_id: str, stage: str, name: str, duration_ms: float,
                status: str = "ok", payload: dict | None = None) -> None:
    """发一条 kind=stage 的 TraceEvent(若无 collector 则静默).

    dispatcher 的内联埋点(共用前置/cache/quota/compress/bridge)用此 helper
    把事件镜像到 TraceCollector。旧的 plugin_trace.append 列表仍保留,
    供 request.state.plugin_trace 向后兼容(后续 Task 再统一收口)。

    若 entry 维度 debug 开关开启,同时镜像一条 kind=debug 事件(payload 填充)。
    """
    from aigateway_core.trace_event import TraceCollector, TraceEvent
    collector = TraceCollector.current()
    if collector:
        collector.emit(TraceEvent(
            trace_id=trace_id,
            ts=time.monotonic(),
            stage=stage,
            kind="stage",
            name=name,
            duration_ms=round(duration_ms, 2),
            status=status,
            payload=payload,
        ))
        # 若 entry 维度 debug 开,镜像 kind=debug 事件(payload 填充)
        collector.emit_debug(stage, name, duration_ms, status, "entry", payload)


def _emit_plugin(trace_id: str, plugin_name: str, duration_ms: float,
                 status: str = "ok") -> None:
    """发一条 kind=plugin 的 TraceEvent(_run_engine_filtered 用)."""
    from aigateway_core.trace_event import TraceCollector, TraceEvent
    collector = TraceCollector.current()
    if collector:
        collector.emit(TraceEvent(
            trace_id=trace_id,
            ts=time.monotonic(),
            stage=plugin_name,
            kind="plugin",
            name=f"{plugin_name}.execute",
            duration_ms=round(duration_ms, 2),
            status=status,
        ))


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

    分类不做路由决策——只判断请求「想干什么」,让 LiteLLM 在管道末端
    决定「用哪个模型」。所以对 model=='auto' 请求,分类器只看模态/意图,
    不试图先解析 auto。

    优先级(高→低):
    1. 显式意图:body.generation_intent == True(可选字段,给客户端一个明确入口)
    2. 模态推断:messages 含 image/audio/video 生成块 → generation
    3. 已知的 generative 模型名(仅当非 auto):body.model 命中 providers
       里标记 generative 的模型 → generation
    4. 默认:understanding(纯文本理解、mllm 多模态理解都走这条)

    Args:
        body: ChatCompletionRequest（或 dict），需有 model 和 messages。
        config_manager: ConfigManager,用于查 providers 模型模态配置。

    Returns:
        "understanding" | "generation"
    """
    model = getattr(body, "model", None) or (body.get("model") if isinstance(body, dict) else None)
    messages = getattr(body, "messages", None) or (body.get("messages") if isinstance(body, dict) else None)
    generation_intent = getattr(body, "generation_intent", None)
    if generation_intent is None and isinstance(body, dict):
        generation_intent = body.get("generation_intent")

    # 1. 显式意图
    if generation_intent is True:
        return "generation"

    # 2. 模态推断
    if messages and _content_modality_hint(messages) == "generation":
        return "generation"

    # 3. 模型名推断(仅当非 auto——auto 的语义就是「你帮我选」,交给 bridge)
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

    # 4. 默认 understanding(含 model=='auto' 的纯文本请求)
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

        总分总架构（采纳 C.3 决策 1「共用前置」+ auto 解析下沉到 LiteLLM）:
        1. 【共用前置】所有请求先跑 media_optimization → PII 检测。
           两者是「输入规范化」——纯输入变换,不涉及路由决策,两条管道都受益
           (生成管道也会脱敏用户 prompt 里粘的 API key/邮箱)。
        2. 【分流】按模态/意图分类,不看 body.model。理由:
           auto 请求最终选哪个模型由 LiteLLM 在管道末端决定,分类器不该越权。
        3. 【管道】按 pipeline_kind 走对应 engine + 后续插件链。
        4. 【末端路由】body.model 原封传给 litellm_bridge。若 model=='auto',
           bridge 内部结合 pipeline 上下文(PII/压缩/RAG 信号)选具体模型
           调 LiteLLM Router;若已指定模型,直接调用+走 fallback 链。

        body.should_stream 决定走流式还是非流式编排。
        """
        from aigateway_api.openai_compat import (
            _apply_media_optimization,
            _apply_pii_detection,
            _record_request_log,
        )

        request_start_time = time.time()
        plugin_trace: list = []
        state = self.state

        # 解析 user_id / key_hash（从鉴权中间件注入）
        user_id, key_hash = self._resolve_identity(request)

        # ===== 共用前置 1: Media Optimization =====
        # 多模态 content(图片/音频/视频)先转文本,PII 才能扫到图片 OCR 出的敏感文本。
        # 注意:生成管道的图片输入(文生图不适用,图生图适用)按理不该 OCR,
        # 但 media_optimization 内部已按 content type 判断,这里放心跑。
        mol_start = time.time()
        mol_result = await _apply_media_optimization(body, request, state)
        body.messages = mol_result["messages"]
        mol_meta = mol_result["meta"]
        if mol_meta:
            _mol_ms = round((time.time() - mol_start) * 1000, 2)
            plugin_trace.append({"plugin_name": "media_optimization",
                                 "duration_ms": _mol_ms,
                                 "status": "success"})
            _emit_stage(request.state.trace_id, "media", "media_optimizer.process", _mol_ms, "ok")

        # ===== 共用前置 2: PII Detection(生成管道也受此保护)=====
        pii_start = time.time()
        pii_result = await _apply_pii_detection(body, request, state)
        if "error" in pii_result:
            _pii_ms = round((time.time() - pii_start) * 1000, 2)
            plugin_trace.append({"plugin_name": "pii_detector",
                                 "duration_ms": _pii_ms,
                                 "status": "rejected"})
            _emit_stage(request.state.trace_id, "pii", "pii_detector.sanitize", _pii_ms, "error")
            request.state.plugin_trace = plugin_trace
            await _record_request_log(request=request, method="POST", endpoint="/v1/chat/completions",
                                      status_code=403, duration_ms=0, model=body.model,
                                      cache_hit=False, cache_tier=None)
            return JSONResponse(content=pii_result["error"], status_code=403)
        body.messages = pii_result["messages"]
        pii_meta = pii_result["meta"]
        if pii_meta:
            _pii_ok_ms = round((time.time() - pii_start) * 1000, 2)
            plugin_trace.append({"plugin_name": "pii_detector",
                                 "duration_ms": _pii_ok_ms,
                                 "status": "success", **pii_meta})
            _emit_stage(request.state.trace_id, "pii", "pii_detector.sanitize", _pii_ok_ms, "ok", payload=pii_meta)

        # 前置结果打包传给下游管道(auto 解析已下沉到 bridge,不传 router_meta)
        prefix = {
            "plugin_trace": plugin_trace,
            "request_start_time": request_start_time,
            "mol_meta": mol_meta,
            "pii_meta": pii_meta,
        }

        # ===== 分流(只看模态和显式意图,不看 body.model)=====
        # auto 请求最终选哪个模型由 bridge 决定,分流器不越权。
        pipeline_kind = classify_request(body, self.config_manager)
        logger.info(
            "dispatch: pipeline_kind=%s, model=%s, stream=%s",
            pipeline_kind,
            getattr(body, "model", None),
            getattr(body, "stream", False),
        )

        engine = self.understanding_engine if pipeline_kind == "understanding" else self.generation_engine

        if pipeline_kind == "understanding":
            return await self._dispatch_understanding(body, request, engine, user_id, key_hash, prefix)
        return await self._dispatch_generation(body, request, engine, user_id, key_hash, prefix)

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
        self, body: Any, request: Request, engine: Any,
        user_id: Optional[str], key_hash: Optional[str], prefix: Dict[str, Any],
    ) -> JSONResponse:
        """理解管道：缓存 → 配额 → engine 插件链 → prompt_compress → LiteLLM → 回填。

        media_optimization / PII / auto 模型解析已由 dispatch() 共用前置完成,
        本方法从 prefix 拿到那三步的 meta 和累计 plugin_trace。
        """
        from aigateway_api.openai_compat import (
            _apply_prompt_compression,
            _record_request_log,
        )

        state = self.state
        plugin_trace: list = prefix["plugin_trace"]
        request_start_time: float = prefix["request_start_time"]
        mol_meta = prefix.get("mol_meta")
        pii_meta = prefix.get("pii_meta")
        # router_meta 由 bridge 在 auto 解析后回填(见 _call_llm_nonstream/stream)
        router_meta: Optional[Dict[str, Any]] = None

        # ===== 缓存查找（engine 之前，避免 RAG 等插件浪费 token）=====
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
        _cache_ms = round((time.time() - cache_start) * 1000, 2)
        plugin_trace.append({"plugin_name": cache_plugin_name,
                             "duration_ms": _cache_ms,
                             "status": "success"})
        _emit_stage(request.state.trace_id, "cache", f"{cache_plugin_name}.lookup", _cache_ms, "ok",
                    payload={"hit_tier": hit_tier} if hit_tier else None)

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
            # ensure_ascii=False 保证 CJK/emoji 按真实字符长度计数,否则 Chinese
            # 字符会被展开成 \uXXXX 六字节序列,导致配额估算膨胀 ~6x
            estimated_tokens = sum(len(json.dumps(m, ensure_ascii=False)) for m in body.messages) // 4
            allowed, fail_msg, retry_after = await key_store.check_quota(
                key_hash=key_hash, tokens=estimated_tokens, cost=0.0
            )
            if not allowed:
                _qfail_ms = round((time.time() - quota_start) * 1000, 2)
                plugin_trace.append({"plugin_name": "quota_check",
                                     "duration_ms": _qfail_ms,
                                     "status": "failed"})
                _emit_stage(request.state.trace_id, "quota", "key_store.check_quota", _qfail_ms, "error")
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
        _qok_ms = round((time.time() - quota_start) * 1000, 2)
        plugin_trace.append({"plugin_name": "quota_check",
                             "duration_ms": _qok_ms,
                             "status": "success"})
        _emit_stage(request.state.trace_id, "quota", "key_store.check_quota", _qok_ms, "ok")

        # ===== 跑理解管道 engine 插件链（rag_retriever / conv_compressor 等）=====
        # 注意：pii/cache/semantic/compress/media 已在
        # dispatch() 共用前置或本方法前面步骤中处理，engine 跑前先过滤掉重复项。
        # （经典 model_router 插件已删除，真路由在 bridge 的 auto 解析；此处无需 skip。）
        if engine is not None:
            try:
                ctx = PipelineContext(
                    request={"messages": body.messages, "model": body.model, "stream": getattr(body, "stream", False)},
                    trace_id=request.state.trace_id,
                    pipeline_kind="understanding",
                    user_id=user_id,
                )
                ctx.should_stream = getattr(body, "stream", False)
                # 过滤掉已被辅助函数处理的核心插件，避免重复执行
                # 注意名字必须与注册名一致（media 注册名为 media_optimizer，
                # 非 media_optimization——曾因写错导致 skip 失效、media 双跑）。
                ctx._skip_names = {"pii_detector", "prompt_cache", "semantic_cache",
                                   "prompt_compress", "media_optimizer"}
                ctx = await self._run_engine_filtered(engine, ctx)
                # 插件链可能改写 messages / model（rag_retriever 追加检索上下文、
                # conv_compressor 摘要历史）——回写到 body，供后续 prompt_compress / LLM 调用使用。
                req = ctx.request
                if isinstance(req, dict):
                    new_messages = req.get("messages")
                    if new_messages:
                        body.messages = new_messages
                    if req.get("model"):
                        body.model = req["model"]
            except Exception as exc:
                logger.warning("理解管道 engine 执行异常（fail-open 继续）: %s", exc)

        # ===== Prompt Compression =====
        compress_start = time.time()
        compress_result = await _apply_prompt_compression(body, request, state)
        body.messages = compress_result["messages"]
        compress_meta = compress_result["meta"]
        if compress_meta and compress_meta.get("compression_ratio", 1.0) < 1.0:
            _comp_ms = round((time.time() - compress_start) * 1000, 2)
            plugin_trace.append({"plugin_name": "prompt_compress",
                                 "duration_ms": _comp_ms,
                                 "status": "success", **compress_meta})
            _emit_stage(request.state.trace_id, "compress", "prompt_compress.compress", _comp_ms, "ok", payload=compress_meta)
        else:
            _comp_skip_ms = round((time.time() - compress_start) * 1000, 2)
            plugin_trace.append({"plugin_name": "prompt_compress",
                                 "duration_ms": _comp_skip_ms,
                                 "status": "skipped"})
            _emit_stage(request.state.trace_id, "compress", "prompt_compress.compress", _comp_skip_ms, "skip")

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
                user_id, key_hash, cache_key, normalized_messages,
                pipeline_kind="understanding",
            )
        return await self._call_llm_nonstream(
            body, request, litellm_bridge, plugin_trace, request_start_time,
            user_id, key_hash, cache_key, normalized_messages,
            pii_meta, router_meta, mol_meta, compress_meta,
            pipeline_kind="understanding",
        )

    # ------------------------------------------------------------------
    # 生成管道编排
    # ------------------------------------------------------------------

    async def _dispatch_generation(
        self, body: Any, request: Request, engine: Any,
        user_id: Optional[str], key_hash: Optional[str], prefix: Dict[str, Any],
    ) -> JSONResponse:
        """生成管道：engine 插件链 → 配额 → LiteLLM（不查理解缓存）。

        media_optimization / PII 已由 dispatch() 共用前置完成,本方法从 prefix
        拿到 meta 和累计 plugin_trace。
        生成结果（图片/视频）缓存语义复杂，本管道默认不查 prompt_cache；
        可选走 MediaCacheManager（本次不接入，留待后续）。
        """
        from aigateway_api.openai_compat import _record_request_log

        state = self.state
        plugin_trace: list = prefix["plugin_trace"]
        request_start_time: float = prefix["request_start_time"]
        mol_meta = prefix.get("mol_meta")
        pii_meta = prefix.get("pii_meta")

        # 跑生成管道 engine 插件链（ai_director → ... → cost_tracker）
        if engine is not None:
            try:
                ctx = PipelineContext(
                    request={"messages": body.messages, "model": body.model,
                             "stream": getattr(body, "stream", False)},
                    trace_id=request.state.trace_id,
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
            # ensure_ascii=False:见 _dispatch_understanding 中的等价注释
            estimated_tokens = sum(len(json.dumps(m, ensure_ascii=False)) for m in body.messages) // 4
            allowed, fail_msg, retry_after = await key_store.check_quota(
                key_hash=key_hash, tokens=estimated_tokens, cost=0.0
            )
            if not allowed:
                _qfail_ms = round((time.time() - quota_start) * 1000, 2)
                plugin_trace.append({"plugin_name": "quota_check",
                                     "duration_ms": _qfail_ms,
                                     "status": "failed"})
                _emit_stage(request.state.trace_id, "quota", "key_store.check_quota", _qfail_ms, "error")
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
        _qok_ms = round((time.time() - quota_start) * 1000, 2)
        plugin_trace.append({"plugin_name": "quota_check",
                             "duration_ms": _qok_ms,
                             "status": "success"})
        _emit_stage(request.state.trace_id, "quota", "key_store.check_quota", _qok_ms, "ok")

        # ===== LiteLLM 出口（生成管道直接调，不查缓存）=====
        # body.model 可能是 'auto',由 bridge 内部按 generation 模态解析。
        litellm_bridge = self.litellm_bridge
        if litellm_bridge is None:
            return JSONResponse(
                content={"error": {"code": "internal_error", "message": "LiteLLM bridge not initialized"}},
                status_code=500,
            )

        if getattr(body, "stream", False):
            return await self._call_llm_stream(
                body, request, litellm_bridge, plugin_trace, request_start_time,
                user_id, key_hash,
                cache_key=None,  # 生成管道不回填
                normalized_messages=None,
                pipeline_kind="generation",
            )
        return await self._call_llm_nonstream(
            body, request, litellm_bridge, plugin_trace, request_start_time,
            user_id, key_hash,
            cache_key=None,  # 生成管道不回填
            normalized_messages=None,
            pii_meta=pii_meta, router_meta=None, mol_meta=mol_meta, compress_meta=None,
            pipeline_kind="generation",
        )

    # ------------------------------------------------------------------
    # LiteLLM 出口（两条管道共用）
    # ------------------------------------------------------------------

    async def _call_llm_nonstream(
        self, body, request, litellm_bridge, plugin_trace, request_start_time,
        user_id, key_hash, cache_key, normalized_messages,
        pii_meta=None, router_meta=None, mol_meta=None, compress_meta=None,
        pipeline_kind: str = "understanding",
    ) -> JSONResponse:
        """非流式调 LiteLLM 出口 + 用量记录 + 缓存回填。

        normalized_messages: 用于 L3 语义缓存回填（生成管道传 None,不做 L3 回填）。
        pipeline_kind: 传给 bridge,body.model=='auto' 时按此选候选池模态。
        """
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
                pipeline_kind=pipeline_kind,
            )
        except Exception as exc:
            _llm_fail_ms = round((time.time() - request_start_time) * 1000, 2)
            plugin_trace.append({"plugin_name": "llm_completion",
                                 "duration_ms": _llm_fail_ms,
                                 "status": "failed"})
            _emit_stage(request.state.trace_id, "bridge", "litellm_bridge.completion", _llm_fail_ms, "error")
            logger.error("LLM completion failed: %s", exc, exc_info=True)
            if tracker:
                tracker.__exit__(type(exc), exc, exc.__traceback__)
            return JSONResponse(
                content={"error": {"code": "internal_error",
                                   "message": f"Upstream completion error: {exc}"}},
                status_code=500,
            )

        _llm_ok_ms = round((time.time() - request_start_time) * 1000, 2)
        plugin_trace.append({"plugin_name": "llm_completion",
                             "duration_ms": _llm_ok_ms,
                             "status": "success"})
        _emit_stage(request.state.trace_id, "bridge", "litellm_bridge.completion", _llm_ok_ms, "ok")

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
                # L3 异步回填（需要 normalized_messages 计算 embedding；缺则跳过）
                if normalized_messages:
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
        # bridge 已在 result["_meta"]["model_router"] 里写好了 auto 解析结果(如有);
        # 如果 dispatcher 层没传 router_meta(通常都不传了),从 bridge 结果拿。
        bridge_router_meta = meta.get("model_router")
        effective_router_meta = router_meta or bridge_router_meta
        meta.update({
            "media_optimization": mol_meta,
            "pii_detector": pii_meta,
            "model_router": effective_router_meta,
            "prompt_compress": compress_meta,
        })
        meta = {k: v for k, v in meta.items() if v}
        return JSONResponse(
            content={"data": result.get("data", {}), "message": "success", "_meta": meta},
            status_code=200,
        )

    async def _call_llm_stream(
        self, body, request, litellm_bridge, plugin_trace, request_start_time,
        user_id, key_hash, cache_key, normalized_messages,
        pipeline_kind: str = "understanding",
    ) -> JSONResponse:
        """流式调 LiteLLM 出口。

        流式修正（对齐非流式）:
        - 扣配额:流结束后从 last_chunk.usage 取 token 调 increment_usage
        - 回填缓存:cache_key 非空时回填 L1/L2/L3（累积真实 chunk 内容，不写空 choices）
        - cost 真实值:优先用 bridge 返回,否则估算
        - llm_completion duration_ms 不写死 0

        normalized_messages: 用于 L3 语义缓存回填（生成管道传 None）。
        pipeline_kind: 传给 bridge,body.model=='auto' 时按此选候选池模态。
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
            pipeline_kind=pipeline_kind,
        )

        # 包装生成器：消费完后做配额扣减 + 缓存回填 + metrics（修正后行为）
        completion_gen = self._wrap_stream_full(
            completion_gen, metrics_collector, cache_manager, key_store,
            body.model, user_id, key_hash, cache_key, normalized_messages, llm_start,
        )

        _stream_ms = round((time.time() - llm_start) * 1000, 2)
        plugin_trace.append({"plugin_name": "llm_completion",
                             "duration_ms": _stream_ms,
                             "status": "success"})
        _emit_stage(request.state.trace_id, "bridge", "litellm_bridge.completion_stream", _stream_ms, "ok")
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
        model, user_id, key_hash, cache_key, normalized_messages, llm_start,
    ):
        """流式包装器:透传 chunk + 末尾做配额/缓存/metrics。

        合并了原 _wrap_stream_for_metrics 的 metrics 逻辑，并新增:
        - increment_usage（原流式不扣，本次修正）
        - 缓存回填（原流式不回填，本次修正）
          回填内容 = 累积所有 chunk 的 delta.content 拼成完整 message，
          与非流式响应格式一致，供后续 simulate_stream_from_cache 回放。
        """
        from aigateway_api.openai_compat import _estimate_cost

        last_chunk = {}
        # 累积每个 choice 的 content / role / tool_calls，用于组装非流式格式
        accum: Dict[int, Dict[str, Any]] = {}
        async for chunk in gen:
            last_chunk = chunk
            # 累积 delta 到 accum（供缓存回填使用）
            if isinstance(chunk, dict):
                for choice in chunk.get("choices", []) or []:
                    if not isinstance(choice, dict):
                        continue
                    idx = choice.get("index", 0)
                    slot = accum.setdefault(idx, {"role": "assistant", "content": ""})
                    delta = choice.get("delta") or {}
                    if isinstance(delta, dict):
                        if delta.get("role"):
                            slot["role"] = delta["role"]
                        piece = delta.get("content")
                        if isinstance(piece, str):
                            slot["content"] += piece
                    fr = choice.get("finish_reason")
                    if fr:
                        slot["finish_reason"] = fr
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

        # 缓存回填（修正点：原流式回填时写空 choices→simulate_stream_from_cache
        # 短路返回 Empty response 错误。本次改成累积真实 delta.content 后回填）
        if cache_key and cache_manager and accum:
            try:
                choices_out = []
                for idx in sorted(accum.keys()):
                    slot = accum[idx]
                    # 内容为空则跳过该 choice（避免依然写出空 message 触发投毒）
                    if not slot.get("content"):
                        continue
                    choices_out.append({
                        "index": idx,
                        "message": {
                            "role": slot.get("role", "assistant"),
                            "content": slot["content"],
                        },
                        "finish_reason": slot.get("finish_reason", "stop"),
                    })
                if not choices_out:
                    return  # 没有可用内容，不投毒
                value_str = json.dumps({
                    "choices": choices_out,
                    "usage": usage,
                    "model": model,
                }, ensure_ascii=False)
                cache_manager.l1_set(cache_key, value_str)
                try:
                    await cache_manager.l2_set(cache_key, value_str)
                except Exception as exc:
                    logger.warning("流式 L2 回填失败: %s", exc)
                # L3 异步回填（与非流式对齐；需要 normalized_messages 计算 embedding）
                if normalized_messages:
                    import asyncio
                    from aigateway_api.openai_compat import _safe_l3_backfill
                    asyncio.create_task(_safe_l3_backfill(
                        cache_manager, cache_key, value_str,
                        normalized_messages, model, user_id or "", tt,
                    ))
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
                _emit_plugin(ctx.trace_id, plugin.name, elapsed, "error")
                logger.warning("插件 %s 执行失败（fail-open）: %s", plugin.name, exc)
                continue
            elapsed = (_time.monotonic() - pstart) * 1000
            _emit_plugin(ctx.trace_id, plugin.name, elapsed, "ok")
        return ctx
