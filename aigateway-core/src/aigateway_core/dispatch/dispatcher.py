"""RequestDispatcher — 总分总架构的「总入口」（core 实现）。

本模块是 RequestDispatcher 的定义归宿（Task 2 迁移）。
aigateway_api.dispatcher 现已退化为 thin adapter，仅 re-export 本模块。

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

import asyncio
import hashlib
import json
import logging
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from aigateway_core.dispatch.classifier import classify_request
from aigateway_core.dispatch.context import PipelineContext

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Cache key v2 辅助函数
# ------------------------------------------------------------------

def _extract_cacheable_context(messages: list, tail: int = 3) -> list:
    """从完整 messages 数组中提取用于计算 cache_key 的最小上下文。

    多轮对话每加一条 assistant 回复,如果把整个 messages 都 hash,
    cache_key 会永远变,导致命中率天然为 0。这里只保留:
    - 所有 system 消息(通常只有 1 条,决定"人设/工具集")
    - 末尾 tail 轮 user/assistant 对话

    这样"末尾提问一致"的多轮对话可以共享 cache_key。tail=3 意味着
    最近 3 轮 exchange 的上下文才影响 key,更早的历史不影响。

    Args:
        messages: OpenAI 格式的完整 messages 数组。
        tail: 保留末尾多少条(不含 system)。默认 3。

    Returns:
        裁剪后的 messages 列表(仍保持顺序)。
    """
    if not messages:
        return []
    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]
    tail_msgs = non_system[-tail:] if len(non_system) > tail else non_system
    return system_msgs + tail_msgs


def _resolve_cache_scope(request: Request, pii_meta: Optional[dict]) -> str:
    """决定本次请求的 cache_scope。

    优先级:
    1. 显式请求头 X-Cache-Scope=private|group|public
    2. PII 检测命中 → 强制 private(避免脱敏后 prompt 泄露到共享缓存)
    3. 默认 group

    Args:
        request: FastAPI Request 对象。
        pii_meta: PII 检测结果(包含 detected_categories)。

    Returns:
        "private" | "group" | "public"。
    """
    hdr = (request.headers.get("X-Cache-Scope") or "").strip().lower()
    if hdr in ("private", "group", "public"):
        return hdr
    # PII 命中自动升 private
    if pii_meta and pii_meta.get("detected_categories"):
        return "private"
    return "group"


def _emit_stage(trace_id: str, stage: str, name: str, duration_ms: float,
                status: str = "ok", payload: dict | None = None) -> None:
    """发一条 kind=stage 的 TraceEvent(若无 collector 则静默).

    dispatcher 的内联埋点(共用前置/cache/quota/compress/bridge)用此 helper
    把事件镜像到 TraceCollector。旧的 plugin_trace.append 列表仍保留,
    供 request.state.plugin_trace 向后兼容(后续 Task 再统一收口)。

    payload 在 debug 开关开启时由调用方填充;不需要额外发 kind=debug 事件,
    否则同一操作会在 trace 里出现两行(stage + debug)。
    """
    from aigateway_core.shared.trace_event import TraceCollector, TraceEvent
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


def _emit_plugin(trace_id: str, plugin_name: str, duration_ms: float,
                 status: str = "ok") -> None:
    """发一条 kind=plugin 的 TraceEvent(_run_engine_filtered 用)."""
    from aigateway_core.shared.trace_event import TraceCollector, TraceEvent
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
        self.intent_classifier = state.get("intent_classifier")

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
            _emit_stage(request.state.trace_id, "media", "media_optimizer.process", _mol_ms, "ok",
                        payload=mol_meta)

        # ===== 共用前置 2: PII Detection(生成管道也受此保护)=====
        pii_start = time.time()
        pii_result = await _apply_pii_detection(body, request, state)
        if "error" in pii_result:
            _pii_ms = round((time.time() - pii_start) * 1000, 2)
            plugin_trace.append({"plugin_name": "pii_detector",
                                 "duration_ms": _pii_ms,
                                 "status": "rejected"})
            _emit_stage(request.state.trace_id, "pii", "pii_detector.sanitize", _pii_ms, "error",
                        payload={"reason": pii_result["error"].get("code", "pii_detected")})
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
        pipeline_kind, intent_hint = await classify_request(
            body, self.config_manager, intent_classifier=self.intent_classifier
        )
        logger.info(
            "dispatch: pipeline_kind=%s, model=%s, stream=%s",
            pipeline_kind,
            getattr(body, "model", None),
            getattr(body, "stream", False),
        )

        engine = self.understanding_engine if pipeline_kind == "understanding" else self.generation_engine

        if pipeline_kind == "understanding":
            return await self._dispatch_understanding(body, request, engine, user_id, key_hash, prefix)
        return await self._dispatch_generation(body, request, engine, user_id, key_hash, prefix, pipeline_kind, intent_hint)

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
        # v2 key:只哈希 system + 末尾 3 轮对话,而非全量 messages。多轮对话
        # 末尾提问一致的请求可共享 cache_key(见 _extract_cacheable_context)。
        cacheable_msgs = _extract_cacheable_context(body.messages, tail=3)
        normalized_messages = json.dumps(cacheable_msgs, sort_keys=True, ensure_ascii=False)
        # cache_scope:X-Cache-Scope 请求头显式指定 or PII 命中自动升 private。
        cache_scope = _resolve_cache_scope(request, pii_meta)
        # Extract group_id from auth data (set by auth_middleware)
        group_id = ""
        if hasattr(request.state, "api_key_data") and request.state.api_key_data:
            group_id = request.state.api_key_data.get("group_id") or ""
        cache_key = cache_manager.generate_cache_key(
            normalized_prompt=normalized_messages,
            model=body.model,
            pipeline_kind="understanding",
            cache_scope=cache_scope,
            user_id=user_id or "",
            group_id=group_id,
            temperature=body.temperature if body.temperature is not None else 1.0,
            max_tokens=body.max_tokens,
            top_p=body.top_p,
        )

        cache_start = time.time()
        cache_kwargs: Dict[str, Any] = {"user_id": user_id}
        if cache_manager._qdrant_client is not None:
            from aigateway_core.prefix.cache.l3_semantic import _compute_l3_vector
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

        # 缓存指标:命中按 tier 分标签,未命中单独计数。/metrics 输出:
        # gateway_cache_hits_total{tier="L1|L2|L3"} 与 gateway_cache_misses_total
        # (v2 改造前 misses 计数点缺失,导致命中率完全不可观测。)
        metrics_collector = self.metrics_collector
        if metrics_collector:
            if cache_hit:
                metrics_collector.inc_cache_hits(tier=hit_tier)
            else:
                metrics_collector.inc_cache_misses()

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
            if allowed:
                request.state._lua_quota_reserved = True
                request.state._lua_reserved_tokens = estimated_tokens
                request.state._lua_reserved_cost = 0.0
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
                is_group = fail_msg.startswith("Group ")
                if "RPM" in fail_msg:
                    code = "rate_limit_group_rpm" if is_group else "rate_limit_rpm"
                elif "TPM" in fail_msg:
                    code = "rate_limit_group_tpm" if is_group else "rate_limit_tpm"
                elif "Daily" in fail_msg:
                    code = "quota_exceeded_group_daily_tokens" if is_group else "quota_exceeded_daily_tokens"
                elif "Monthly" in fail_msg:
                    code = "quota_exceeded_group_monthly_cost" if is_group else "quota_exceeded_monthly_cost"
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
                ctx.extra["cache_scope"] = cache_scope
                ctx.extra["group_id"] = group_id
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
                pipeline_kind="understanding", group_id=group_id,
            )
        return await self._call_llm_nonstream(
            body, request, litellm_bridge, plugin_trace, request_start_time,
            user_id, key_hash, cache_key, normalized_messages,
            pii_meta, router_meta, mol_meta, compress_meta,
            pipeline_kind="understanding", group_id=group_id,
        )

    # ------------------------------------------------------------------
    # 生成管道编排
    # ------------------------------------------------------------------

    async def _dispatch_generation(
        self, body: Any, request: Request, engine: Any,
        user_id: Optional[str], key_hash: Optional[str], prefix: Dict[str, Any],
        pipeline_kind: str = "generation:image",
        intent_hint: Optional[str] = None,
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

        # Extract group_id from auth data; compute resolved_scope from PII + header
        group_id = ""
        if hasattr(request.state, "api_key_data") and request.state.api_key_data:
            group_id = request.state.api_key_data.get("group_id") or ""

        resolved_scope = _resolve_cache_scope(request, pii_meta)

        # ===== 配额检查 =====
        # 必须在 engine 执行之前做：草稿预览(draft_generator)会在 engine 内真实调用
        # Agnes 出图、消耗 token，若先出图再检查配额则预览生成不计费、可被无限刷。
        # 估算基于原始 messages(engine 可能改写 prompt，但配额本就是粗估 ÷4)。
        key_store = self.key_store
        quota_start = time.time()
        if key_hash and key_store:
            # ensure_ascii=False:见 _dispatch_understanding 中的等价注释
            estimated_tokens = sum(len(json.dumps(m, ensure_ascii=False)) for m in body.messages) // 4
            allowed, fail_msg, retry_after = await key_store.check_quota(
                key_hash=key_hash, tokens=estimated_tokens, cost=0.0
            )
            if allowed:
                request.state._lua_quota_reserved = True
                request.state._lua_reserved_tokens = estimated_tokens
                request.state._lua_reserved_cost = 0.0
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
                is_group = fail_msg.startswith("Group ")
                if "RPM" in fail_msg:
                    code = "rate_limit_group_rpm" if is_group else "rate_limit_rpm"
                elif "TPM" in fail_msg:
                    code = "rate_limit_group_tpm" if is_group else "rate_limit_tpm"
                elif "Daily" in fail_msg:
                    code = "quota_exceeded_group_daily_tokens" if is_group else "quota_exceeded_daily_tokens"
                elif "Monthly" in fail_msg:
                    code = "quota_exceeded_group_monthly_cost" if is_group else "quota_exceeded_monthly_cost"
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

        # 跑生成管道 engine 插件链（ai_director → ... → cost_tracker）
        ctx: Optional[PipelineContext] = None
        if engine is not None:
            try:
                ctx = PipelineContext(
                    request={"messages": body.messages, "model": body.model,
                             "stream": getattr(body, "stream", False)},
                    trace_id=request.state.trace_id,
                    pipeline_kind=pipeline_kind,
                    user_id=user_id,
                )
                ctx.extra["cache_scope"] = resolved_scope
                ctx.extra["group_id"] = group_id
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
                ctx = None  # fail-open: continue without engine results

        # ===== Draft 确认门控：草稿已生成则返回 preview，等待用户确认后再 upscale =====
        # 配额已在 engine 之前扣过，预览生成计费正确。
        if ctx is not None:
            gen_opt = getattr(ctx, 'extra', {}) or {}
            draft_info = gen_opt.get("generation_optimization", {}).get("draft_generator", {})
            if draft_info.get("applicable") and draft_info.get("draft_id"):
                draft_id = draft_info["draft_id"]
                _emit_stage(request.state.trace_id, "draft", "draft_workflow.pending_confirmation", 0, "ok")
                plugin_trace.append({"plugin_name": "draft_workflow",
                                     "duration_ms": 0,
                                     "status": "pending_confirmation"})
                request.state.plugin_trace = plugin_trace
                return JSONResponse(content={
                    "data": {
                        "draft_id": draft_id,
                        "preview_url": f"/admin/draft/{draft_id}/preview",
                        "generation_params": draft_info.get("generation_params", {}),
                    },
                    "_meta": {"draft_pending_confirmation": True},
                })

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
                pipeline_kind=pipeline_kind, group_id=group_id,
                intent_hint=intent_hint,
            )
        return await self._call_llm_nonstream(
            body, request, litellm_bridge, plugin_trace, request_start_time,
            user_id, key_hash,
            cache_key=None,  # 生成管道不回填
            normalized_messages=None,
            pii_meta=pii_meta, router_meta=None, mol_meta=mol_meta, compress_meta=None,
            pipeline_kind=pipeline_kind, group_id=group_id,
            intent_hint=intent_hint,
        )

    # ------------------------------------------------------------------
    # LiteLLM 出口（两条管道共用）
    # ------------------------------------------------------------------

    async def _call_llm_nonstream(
        self, body, request, litellm_bridge, plugin_trace, request_start_time,
        user_id, key_hash, cache_key, normalized_messages,
        pii_meta=None, router_meta=None, mol_meta=None, compress_meta=None,
        pipeline_kind: str = "understanding",
        group_id: str = "",
        intent_hint: Optional[str] = None,
    ) -> JSONResponse:
        """非流式调 LiteLLM 出口 + 用量记录 + 缓存回填。

        normalized_messages: 用于 L3 语义缓存回填（生成管道传 None,不做 L3 回填）。
        pipeline_kind: 传给 bridge,body.model=='auto' 时按此选候选池模态。
        """
        from aigateway_core.route.metrics.costing import _estimate_cost
        from aigateway_api.openai_compat import _record_request_log

        metrics_collector = self.metrics_collector
        cache_manager = self.cache_manager
        key_store = self.key_store

        tracker = None
        if metrics_collector:
            tracker = metrics_collector.track_request("/v1/chat/completions", method="POST")
            tracker.__enter__()

        # Inject trace context for downstream LLM calls
        from aigateway_core.shared.tracing import TracingManager
        extra_headers: Dict[str, str] = {}
        TracingManager.inject_trace_context(
            headers=extra_headers,
            trace_id=request.state.trace_id,
            span_id=getattr(request.state, "request_id", request.state.trace_id),
        )

        try:
            hint = intent_hint
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
                intent=pipeline_kind,
                model_hint=hint,
                extra_headers=extra_headers,
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
                    _lua_already_incr=getattr(request.state, "_lua_quota_reserved", False),
                    _reserved_tokens=getattr(request.state, "_lua_reserved_tokens", 0),
                    _reserved_cost=getattr(request.state, "_lua_reserved_cost", 0.0),
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
                metrics_collector.record_cost(
                    final_cost, model=body.model, user_id=user_id or "", group_id=group_id,
                )

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
                    from aigateway_core.prefix.cache.l3_semantic import _safe_l3_backfill
                    _l3_backfill_task = asyncio.create_task(_safe_l3_backfill(
                        cache_manager, cache_key, value_str,
                        normalized_messages, body.model, user_id or "", tt,
                    ))
                    _l3_backfill_task.add_done_callback(
                        lambda t: logger.warning("L3 异步回填异常: %s", t.exception())
                        if t.exception() else None
                    )
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
        group_id: str = "",
        intent_hint: Optional[str] = None,
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
        from aigateway_core.route.metrics.costing import _estimate_cost
        from aigateway_api.openai_compat import _record_request_log
        from aigateway_api.streaming import create_sse_response

        metrics_collector = self.metrics_collector
        cache_manager = self.cache_manager
        key_store = self.key_store

        llm_start = time.time()
        hint = intent_hint

        # Inject trace context for downstream LLM calls
        from aigateway_core.shared.tracing import TracingManager
        extra_headers: Dict[str, str] = {}
        TracingManager.inject_trace_context(
            headers=extra_headers,
            trace_id=request.state.trace_id,
            span_id=getattr(request.state, "request_id", request.state.trace_id),
        )

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
            intent=pipeline_kind,
            model_hint=hint,
            extra_headers=extra_headers,
        )

        # 包装生成器：消费完后做配额扣减 + 缓存回填 + metrics（修正后行为）
        completion_gen = self._wrap_stream_full(
            completion_gen, metrics_collector, cache_manager, key_store,
            request, body.model, user_id, key_hash, cache_key, normalized_messages, llm_start,
            group_id,
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
        self, gen, metrics_collector, cache_manager, key_store, request,
        model, user_id, key_hash, cache_key, normalized_messages, llm_start,
        group_id="",
    ):
        """流式包装器:透传 chunk + 末尾做配额/缓存/metrics。

        合并了原 _wrap_stream_for_metrics 的 metrics 逻辑，并新增:
        - increment_usage（原流式不扣，本次修正）
        - 缓存回填（原流式不回填，本次修正）
          回填内容 = 累积所有 chunk 的 delta.content 拼成完整 message，
          与非流式响应格式一致，供后续 simulate_stream_from_cache 回放。
        """
        from aigateway_core.route.metrics.costing import _estimate_cost

        last_chunk = {}
        # 累积每个 choice 的 content / role / tool_calls，用于组装非流式格式
        accum: Dict[int, Dict[str, Any]] = {}
        client_disconnected = False
        try:
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
        except asyncio.CancelledError:
            # Client disconnected. Explicitly close the upstream generator so
            # the provider connection is released promptly rather than waiting
            # on GC. Post-processing below still runs (quota/metrics) using
            # whatever we accumulated, but we skip cache backfill — a partial
            # stream is not a complete response and would poison the cache.
            client_disconnected = True
            aclose = getattr(gen, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:
                    pass

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
                metrics_collector.record_cost(
                    final_cost, model=model, user_id=user_id or "", group_id=group_id,
                )

        # 配额扣减（修正点：原流式不扣）
        if key_hash and key_store and tt > 0:
            try:
                await key_store.increment_usage(
                    key_hash, tokens=tt, cost=_estimate_cost(model, tt),
                    model=model, tokens_in=pt, tokens_out=ct,
                    _lua_already_incr=getattr(request.state, "_lua_quota_reserved", False),
                    _reserved_tokens=getattr(request.state, "_lua_reserved_tokens", 0),
                    _reserved_cost=getattr(request.state, "_lua_reserved_cost", 0.0),
                )
            except Exception as exc:
                logger.warning("流式 increment_usage 失败: %s", exc)

        # 缓存回填（修正点：原流式回填时写空 choices→simulate_stream_from_cache
        # 短路返回 Empty response 错误。本次改成累积真实 delta.content 后回填）
        # Skip on client disconnect — accum is a partial stream, not a complete
        # response; caching it would poison the cache for future requests.
        if cache_key and cache_manager and accum and not client_disconnected:
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
                    logger.debug("流式缓存回填跳过: 无可用内容 (tool-call-only response)")
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
                    from aigateway_core.prefix.cache.l3_semantic import _safe_l3_backfill
                    _l3_backfill_task = asyncio.create_task(_safe_l3_backfill(
                        cache_manager, cache_key, value_str,
                        normalized_messages, model, user_id or "", tt,
                    ))
                    _l3_backfill_task.add_done_callback(
                        lambda t: logger.warning("L3 异步回填异常: %s", t.exception())
                        if t.exception() else None
                    )
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
        from aigateway_core.route.streaming.cache_stream import simulate_stream_from_cache
        from aigateway_api.streaming import create_sse_response

        metrics_collector = self.metrics_collector
        cache_duration_ms = round((time.time() - request_start_time) * 1000, 1)

        if getattr(body, "stream", False):
            chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
            stream_gen = simulate_stream_from_cache(cached["value"], hit_tier=hit_tier)
            if metrics_collector:
                # 注:inc_cache_hits 已在 _dispatch_understanding 缓存查找块统一
                # 打点,这里不再重复打(否则会双倍计数)。仅记录请求 + 节省 token。
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
            # 注:inc_cache_hits 已在 _dispatch_understanding 缓存查找块统一
            # 打点,这里不再重复打(否则会双倍计数)。
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
        # Snapshot ordered plugins to protect against in-flight mutation
        # (e.g. a plugin unregistering itself during hot-reload).
        for plugin in list(engine._ordered_plugins):
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
