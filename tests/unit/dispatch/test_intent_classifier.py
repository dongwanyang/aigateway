import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.dispatch.intent_classifier import IntentClassifier


def _mock_bridge(text_model="agnes-2.0-flash"):
    bridge = MagicMock()
    bridge.completion = AsyncMock()
    selector = MagicMock()
    selector.select_text_model = AsyncMock(return_value=text_model)
    return bridge, selector


def _resp(content: str):
    return {"data": {"choices": [{"message": {"content": content}}]}, "_meta": {}}


@pytest.mark.asyncio
async def test_classify_image():
    bridge, sel = _mock_bridge()
    bridge.completion.return_value = _resp('{"generation":"image","hint":"None"}')
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={"timeout_seconds": 3})
    result = await ic.classify(messages=[{"role": "user", "content": "帮我画一只猫"}], body_model="agnes-2.0-flash")
    assert result["generation"] == "image"
    assert result["hint"] == "None"
    assert result["task_profile"]["operation"] == "general"
    # 预判调用必须显式传文本模型 + intent=understanding, 不触发智能路由
    call_kwargs = bridge.completion.call_args.kwargs
    assert call_kwargs.get("model") == "agnes-2.0-flash"
    assert call_kwargs.get("intent") == "understanding"


@pytest.mark.asyncio
async def test_classify_video_with_hint():
    bridge, sel = _mock_bridge()
    bridge.completion.return_value = _resp('{"generation":"video","hint":"agnes-video-v2.0"}')
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={})
    result = await ic.classify(messages=[{"role": "user", "content": "用 agnes-video 生成一段视频"}], body_model=None)
    assert result["generation"] == "video"
    assert result["hint"] == "agnes-video-v2.0"


@pytest.mark.asyncio
async def test_classify_understanding():
    bridge, sel = _mock_bridge()
    bridge.completion.return_value = _resp('{"generation":"understanding","hint":"None"}')
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={})
    result = await ic.classify(messages=[{"role": "user", "content": "解释这段代码"}], body_model=None)
    assert result["generation"] == "understanding"
    assert "hint" in result


@pytest.mark.asyncio
async def test_llm_returns_validated_multidimensional_profile():
    bridge, sel = _mock_bridge()
    bridge.completion.return_value = _resp(
        '{"generation":"understanding","hint":"None","task_profile":{'
        '"operation":"reasoning","domain":"finance","modalities":["text"],'
        '"complexity":82,"requirements":["long_context"],"confidence":0.91}}'
    )
    ic = IntentClassifier(
        bridge=bridge,
        model_selector=sel,
        config={"fast_path_enabled": False},
    )
    result = await ic.classify(
        messages=[{"role": "user", "content": "分析这份财报中的风险"}],
        body_model=None,
    )
    assert result["task_profile"]["operation"] == "reasoning"
    assert result["task_profile"]["domain"] == "finance"
    assert result["task_profile"]["complexity"] == 82
    assert result["classification_source"] == "llm"


@pytest.mark.asyncio
async def test_fast_path_skips_llm_for_high_signal_coding():
    bridge, sel = _mock_bridge()
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={})
    result = await ic.classify(
        messages=[{"role": "user", "content": "帮我写一个 Python 函数"}],
        body_model=None,
    )
    assert result["task_profile"]["operation"] == "coding"
    assert result["classification_source"] == "fast_rule"
    bridge.completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_llm_classifier_receives_recent_user_context():
    bridge, sel = _mock_bridge()
    bridge.completion.return_value = _resp(
        '{"generation":"understanding","hint":"None","task_profile":{'
        '"operation":"coding","domain":"software","modalities":["text"],'
        '"complexity":60,"requirements":[],"confidence":0.9}}'
    )
    ic = IntentClassifier(
        bridge=bridge,
        model_selector=sel,
        config={"fast_path_enabled": False},
    )
    await ic.classify(
        messages=[
            {"role": "user", "content": "这是一个 Python 服务"},
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "继续修复"},
        ],
        body_model=None,
    )
    prompt = bridge.completion.call_args.kwargs["messages"][1]["content"]
    assert "Python 服务" in prompt
    assert "继续修复" in prompt


@pytest.mark.asyncio
async def test_successful_llm_classification_is_cached():
    bridge, sel = _mock_bridge()
    bridge.completion.return_value = _resp(
        '{"generation":"understanding","hint":"None","task_profile":{'
        '"operation":"general","domain":"general","modalities":["text"],'
        '"complexity":40,"requirements":[],"confidence":0.8}}'
    )
    ic = IntentClassifier(
        bridge=bridge,
        model_selector=sel,
        config={
            "fast_path_enabled": False,
            "cache_ttl_seconds": 60,
            "cache_max_entries": 2,
        },
    )
    messages = [{"role": "user", "content": "你好，介绍一下自己"}]
    first = await ic.classify(messages=messages, body_model=None)
    second = await ic.classify(messages=messages, body_model=None)
    assert first == second
    assert bridge.completion.await_count == 1


@pytest.mark.asyncio
async def test_timeout_fallback_heuristic_text():
    bridge, sel = _mock_bridge()
    import asyncio as _a
    async def slow(*a, **k):
        await _a.sleep(5)
    bridge.completion = AsyncMock(side_effect=slow)
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={"timeout_seconds": 0.1})
    result = await ic.classify(messages=[{"role": "user", "content": "你好"}], body_model=None)
    # 纯文本降级 -> understanding
    assert result["generation"] == "understanding"
    assert "hint" in result


@pytest.mark.asyncio
async def test_timeout_fallback_heuristic_image_input_is_understanding():
    """带图片输入块(无生成关键词)降级为 understanding, 不是 image.

    "描述这张图"这类 mllm 理解请求带图输入,不应误判为图片生成。
    旧启发式"带图→image"会把这类请求错误路由到 _do_image_generation。
    """
    bridge, sel = _mock_bridge()
    import asyncio as _a
    async def slow(*a, **k):
        await _a.sleep(5)
    bridge.completion = AsyncMock(side_effect=slow)
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={"timeout_seconds": 0.1})
    msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]
    result = await ic.classify(messages=msgs, body_model=None)
    assert result["generation"] == "understanding"


@pytest.mark.asyncio
async def test_timeout_fallback_heuristic_generation_keyword():
    """用户文本含"画"等生成关键词时降级为 image."""
    bridge, sel = _mock_bridge()
    import asyncio as _a
    async def slow(*a, **k):
        await _a.sleep(5)
    bridge.completion = AsyncMock(side_effect=slow)
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={"timeout_seconds": 0.1})
    result = await ic.classify(messages=[{"role": "user", "content": "画一只猫"}], body_model=None)
    assert result["generation"] == "image"

    result_v = await ic.classify(messages=[{"role": "user", "content": "生成视频"}], body_model=None)
    assert result_v["generation"] == "video"


@pytest.mark.asyncio
async def test_timeout_fallback_heuristic_image_to_video_request():
    """基于图片内容生成视频这类自然语言也应判为 video."""
    bridge, sel = _mock_bridge()
    import asyncio as _a

    async def slow(*a, **k):
        await _a.sleep(5)

    bridge.completion = AsyncMock(side_effect=slow)
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={"timeout_seconds": 0.1})
    result = await ic.classify(
        messages=[{"role": "user", "content": "根据这张图生成一个视频"}],
        body_model=None,
    )
    assert result["generation"] == "video"


@pytest.mark.asyncio
async def test_timeout_fallback_heuristic_english_video_prompt():
    """英文自然语言'generate a 10-second video ...'也应判为 video."""
    bridge, sel = _mock_bridge()
    import asyncio as _a

    async def slow(*a, **k):
        await _a.sleep(5)

    bridge.completion = AsyncMock(side_effect=slow)
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={"timeout_seconds": 0.1})
    result = await ic.classify(
        messages=[{"role": "user", "content": "Please generate a 10-second video about a sunset over the ocean"}],
        body_model=None,
    )
    assert result["generation"] == "video"


@pytest.mark.asyncio
async def test_malformed_json_fallback():
    bridge, sel = _mock_bridge()
    bridge.completion.return_value = _resp("not json at all")
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={})
    result = await ic.classify(messages=[{"role": "user", "content": "画图"}], body_model=None)
    assert result["generation"] in ("understanding", "image")  # 降级不崩
    assert "hint" in result
