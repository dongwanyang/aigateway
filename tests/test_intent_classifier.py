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
    assert result == {"generation": "image", "hint": "None"}
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
    assert result == {"generation": "video", "hint": "agnes-video-v2.0"}


@pytest.mark.asyncio
async def test_classify_understanding():
    bridge, sel = _mock_bridge()
    bridge.completion.return_value = _resp('{"generation":"understanding","hint":"None"}')
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={})
    result = await ic.classify(messages=[{"role": "user", "content": "解释这段代码"}], body_model=None)
    assert result["generation"] == "understanding"
    assert "hint" in result


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
async def test_malformed_json_fallback():
    bridge, sel = _mock_bridge()
    bridge.completion.return_value = _resp("not json at all")
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={})
    result = await ic.classify(messages=[{"role": "user", "content": "画图"}], body_model=None)
    assert result["generation"] in ("understanding", "image")  # 降级不崩
    assert "hint" in result
