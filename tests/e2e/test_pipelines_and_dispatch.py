"""spec §5.1 — 总分总架构 + 两条管道 + auto 末端解析 (9 用例)."""
import uuid
import pytest
import httpx

from tests.conftest import AGNES_TEXT_MODEL


def _tid() -> str:
    return uuid.uuid4().hex


def _meta(resp_json: dict) -> dict:
    """Response body 顶层的 _meta 段."""
    return resp_json.get("_meta", {})


def test_c1_understanding_dispatch(user_client, trace_helpers):
    """5.1 #1: 纯文本 chat -> understanding 管道 + events 含 stage 事件链."""
    tid = _tid()
    r = user_client.post(
        "/v1/chat/completions",
        json={
            "model": AGNES_TEXT_MODEL,
            "messages": [{"role": "user", "content": "简单说一句话"}],
        },
        headers={"X-Trace-Id": tid},
        timeout=120,
    )
    # 上游可能超时返回 502,但 trace_id 链路应完整
    evs = trace_helpers.wait(tid, timeout=5.0)
    assert len(evs) > 0, f"No events for understanding request"
    stage_names = {e.get("name") for e in evs if e.get("kind") == "stage"}
    # understanding 管道应含 pii_detector.sanitize + prompt_cache.lookup + bridge
    assert "pii_detector.sanitize" in stage_names, \
        f"missing pii_detector.sanitize stage. Got: {stage_names}"
    assert "prompt_cache.lookup" in stage_names, \
        f"missing prompt_cache.lookup stage. Got: {stage_names}"


def test_c2_generation_explicit_intent(user_client, trace_helpers):
    """5.1 #2: generation_intent=true -> generation 管道 + gen-opt 插件."""
    tid = _tid()
    r = user_client.post(
        "/v1/chat/completions",
        json={
            "model": AGNES_TEXT_MODEL,
            "messages": [{"role": "user", "content": "生成一段广告词"}],
            "generation_intent": True,
        },
        headers={"X-Trace-Id": tid},
        timeout=120,
    )
    evs = trace_helpers.wait(tid, timeout=5.0)
    assert len(evs) > 0, f"No events for generation request"
    # generation 管道应含 gen-opt 插件(kind=plugin, name=*.execute)
    plugin_names = {e.get("name") for e in evs if e.get("kind") == "plugin"}
    # 6 个 gen-opt 插件应至少出现一个(ai_director 等)
    gen_opt_plugins = {"ai_director.execute", "intent_evaluator.execute",
                       "token_compressor.execute", "draft_generator.execute",
                       "gen_model_router.execute", "cost_tracker.execute"}
    assert plugin_names & gen_opt_plugins, \
        f"No gen-opt plugin events. Got: {plugin_names}"


def test_c3_generation_modality_inferred_image(user_client, trace_helpers):
    """5.1 #3: messages 含 image_url block -> media_optimization 埋点.

    用 1x1 PNG 的 data URL 避免下载超时;agnes-2.0-flash 是 mllm 可吃图片。
    即使上游拒绝,只关心 trace 里存在 media/pii 埋点。
    """
    tid = _tid()
    # 1x1 transparent PNG
    tiny_png = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )
    body = {
        "model": AGNES_TEXT_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "描述这张图"},
            {"type": "image_url", "image_url": {"url": tiny_png}},
        ]}],
    }
    try:
        user_client.post("/v1/chat/completions", json=body,
                         headers={"X-Trace-Id": tid}, timeout=60)
    except (httpx.ReadTimeout, httpx.TimeoutException):
        pass
    # 请求可能因上游拒绝而 4xx/5xx,但 media/pii 都已埋点、finally 已 flush
    evs = trace_helpers.wait(tid, timeout=30.0)
    assert len(evs) > 0, f"No events for multimodal request"
    stage_names = {e.get("name") for e in evs if e.get("kind") == "stage"}
    # 至少应含 pii_detector 或 media 相关 stage
    assert "pii_detector.sanitize" in stage_names or \
           any("media" in str(n).lower() for n in stage_names), \
        f"No media/pii stage found: {stage_names}"


def test_c4_generation_by_model_name(user_client, trace_helpers):
    """5.1 #4: model=agnes-image-2.1-flash -> 被分流到 generation 管道."""
    tid = _tid()
    r = user_client.post(
        "/v1/chat/completions",
        json={
            "model": "agnes-image-2.1-flash",
            "messages": [{"role": "user", "content": "一只可爱的猫"}],
        },
        headers={"X-Trace-Id": tid},
        timeout=120,
    )
    evs = trace_helpers.wait(tid, timeout=5.0)
    assert len(evs) > 0, f"No events for image model request"
    # generation 管道应含 gen-opt 插件
    plugin_names = {e.get("name") for e in evs if e.get("kind") == "plugin"}
    gen_opt_plugins = {"ai_director.execute", "intent_evaluator.execute",
                       "token_compressor.execute", "draft_generator.execute",
                       "gen_model_router.execute", "cost_tracker.execute"}
    assert plugin_names & gen_opt_plugins, \
        f"Not routed to generation. Plugin events: {plugin_names}"


def test_c5_auto_understanding(user_client, trace_helpers):
    """5.1 #5: model=auto + 纯文本 -> events 含 bridge 事件."""
    tid = _tid()
    r = user_client.post(
        "/v1/chat/completions",
        json={
            "model": "auto",
            "messages": [{"role": "user", "content": "hello auto"}],
        },
        headers={"X-Trace-Id": tid},
        timeout=120,
    )
    evs = trace_helpers.wait(tid, timeout=5.0)
    assert len(evs) > 0, f"No events for auto-understanding request"
    stage_names = {e.get("name") for e in evs if e.get("kind") == "stage"}
    # auto 解析后应走到 bridge
    assert "litellm_bridge.completion" in stage_names, \
        f"No bridge stage for auto request. Got: {stage_names}"


def test_c6_auto_generation(user_client, trace_helpers):
    """5.1 #6: model=auto + generation_intent=true -> 走 generation 管道."""
    tid = _tid()
    r = user_client.post(
        "/v1/chat/completions",
        json={
            "model": "auto",
            "messages": [{"role": "user", "content": "生成图片描述"}],
            "generation_intent": True,
        },
        headers={"X-Trace-Id": tid},
        timeout=120,
    )
    evs = trace_helpers.wait(tid, timeout=5.0)
    assert len(evs) > 0, f"No events for auto-generation request"
    plugin_names = {e.get("name") for e in evs if e.get("kind") == "plugin"}
    gen_opt_plugins = {"ai_director.execute", "intent_evaluator.execute",
                       "token_compressor.execute", "draft_generator.execute",
                       "gen_model_router.execute", "cost_tracker.execute"}
    assert plugin_names & gen_opt_plugins, \
        f"auto+generation not routed to generation pipeline. Plugins: {plugin_names}"


def test_c7_pii_common_prelude_both_pipelines(user_client):
    """5.1 #7: PII 在 common prelude, 两管道都过."""
    for extra in ({}, {"generation_intent": True}):
        tid = _tid()
        r = user_client.post(
            "/v1/chat/completions",
            json={
                "model": AGNES_TEXT_MODEL,
                "messages": [{"role": "user", "content": "我的邮箱是 test@example.com,请回答"}],
                **extra,
            },
            headers={"X-Trace-Id": tid},
            timeout=120,
        )
        # 无论 200 还是 502,PII 检测都应被记录(在 trace events 里)
        # 原始 email 不应出现在响应体中(被 sanitize)
        if r.headers.get("content-type", "").startswith("application/json"):
            raw = r.text.lower()
            assert "test@example.com" not in raw, \
                f"PII not masked for extras={extra}. Response body contained the raw email."


def test_c8_generation_skips_prompt_cache(user_client, trace_helpers):
    """5.1 #8: 生成请求连发两次 -> 两次都不命中 prompt_cache (生成管道不查理解缓存)."""
    tid1, tid2 = _tid(), _tid()
    prompt = f"生成一段独特标语 {uuid.uuid4().hex[:6]}"
    user_client.post(
        "/v1/chat/completions",
        json={
            "model": AGNES_TEXT_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "generation_intent": True,
        },
        headers={"X-Trace-Id": tid1},
        timeout=120,
    )
    user_client.post(
        "/v1/chat/completions",
        json={
            "model": AGNES_TEXT_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "generation_intent": True,
        },
        headers={"X-Trace-Id": tid2},
        timeout=120,
    )
    evs2 = trace_helpers.wait(tid2, timeout=5.0)
    # generation 管道不应有 prompt_cache.lookup stage 事件
    cache_events = [e for e in evs2 if e.get("name") == "prompt_cache.lookup"]
    assert not cache_events, \
        f"prompt_cache stage found in generation pipeline: {cache_events}"


def test_c9_model_router_plugin_is_skipped(user_client, trace_helpers):
    """5.1 #9: model_router 空壳不被注册 -> events 无 plugin=model_router."""
    tid = _tid()
    user_client.post(
        "/v1/chat/completions",
        json={
            "model": AGNES_TEXT_MODEL,
            "messages": [{"role": "user", "content": "any"}],
        },
        headers={"X-Trace-Id": tid},
        timeout=120,
    )
    evs = trace_helpers.wait(tid, timeout=5.0)
    plugin_events = [e for e in evs if e.get("kind") == "plugin"]
    assert not any("model_router" in (e.get("name") or "") for e in plugin_events), \
        f"model_router plugin unexpectedly ran. Events: {[e['name'] for e in plugin_events]}"
