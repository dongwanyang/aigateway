r"""Coverage for IntentClassifier._heuristic regex branches (video + image).

The branch expanded the video/image heuristic with regex patterns:
- Chinese: r"(生成|做|制作|创作).{0,20}视频" and r"(生成|画|做|制作|创作).{0,20}(图|图片|图像)"
- English: r"\b(generate|create|make|produce)(?:\s+(?:a|an|the))?.{0,20}\b(video|clip|animation)\b"

Existing tests cover keyword + a couple regex hits; these cover the remaining
branches: clip/animation/produce verbs, Chinese 做/制作/创作 verbs, and the
image regex (vs keyword) path.
"""

import asyncio as _a
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.dispatch.intent_classifier import IntentClassifier


def _mock_bridge():
    bridge = MagicMock()
    bridge.completion = AsyncMock()
    selector = MagicMock()
    selector.select_text_model = AsyncMock(return_value="agnes-2.0-flash")
    return bridge, selector


def _slow_bridge():
    """Bridge whose completion hangs so classify() falls back to heuristic."""
    bridge, sel = _mock_bridge()

    async def slow(*a, **k):
        await _a.sleep(5)

    bridge.completion = AsyncMock(side_effect=slow)
    return bridge, sel


@pytest.mark.asyncio
async def test_heuristic_english_clip():
    """English 'produce a clip' matches _VIDEO_GEN_PATTERN (clip branch)."""
    bridge, sel = _slow_bridge()
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={"timeout_seconds": 0.1})
    result = await ic.classify(
        messages=[{"role": "user", "content": "produce a short clip of the city"}],
        body_model=None,
    )
    assert result["generation"] == "video"


@pytest.mark.asyncio
async def test_heuristic_english_animation():
    """English 'create an animation' matches _VIDEO_GEN_PATTERN (animation branch)."""
    bridge, sel = _slow_bridge()
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={"timeout_seconds": 0.1})
    result = await ic.classify(
        messages=[{"role": "user", "content": "create an animation of ocean waves"}],
        body_model=None,
    )
    assert result["generation"] == "video"


@pytest.mark.asyncio
async def test_heuristic_chinese_zuo_video():
    """Chinese '做视频' (做 verb) matches the video regex."""
    bridge, sel = _slow_bridge()
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={"timeout_seconds": 0.1})
    result = await ic.classify(
        messages=[{"role": "user", "content": "帮我做一个视频"}],
        body_model=None,
    )
    assert result["generation"] == "video"


@pytest.mark.asyncio
async def test_heuristic_chinese_zhizuo_video():
    """Chinese '制作视频' (制作 verb) matches the video regex."""
    bridge, sel = _slow_bridge()
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={"timeout_seconds": 0.1})
    result = await ic.classify(
        messages=[{"role": "user", "content": "制作视频，内容是日落"}],
        body_model=None,
    )
    assert result["generation"] == "video"


@pytest.mark.asyncio
async def test_heuristic_chinese_chuangzuo_video():
    """Chinese '创作一段视频' (创作 verb) matches the video regex with distance."""
    bridge, sel = _slow_bridge()
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={"timeout_seconds": 0.1})
    result = await ic.classify(
        messages=[{"role": "user", "content": "请创作一段视频"}],
        body_model=None,
    )
    assert result["generation"] == "video"


@pytest.mark.asyncio
async def test_heuristic_image_regex_zhizuo():
    """Image regex (制作...图) fires for image generation without keyword match."""
    bridge, sel = _slow_bridge()
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={"timeout_seconds": 0.1})
    # '制作一张图' — not in _IMAGE_GEN_KEYWORDS, but image_pattern matches
    result = await ic.classify(
        messages=[{"role": "user", "content": "请制作一张图给我看看"}],
        body_model=None,
    )
    assert result["generation"] == "image"


@pytest.mark.asyncio
async def test_heuristic_image_regex_zuo_tupian():
    """Image regex (做...图片) fires for '做个图片'."""
    bridge, sel = _slow_bridge()
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={"timeout_seconds": 0.1})
    result = await ic.classify(
        messages=[{"role": "user", "content": "做个图片看看"}],
        body_model=None,
    )
    assert result["generation"] == "image"


@pytest.mark.asyncio
async def test_heuristic_pure_understanding_no_gen_verb():
    """Plain text with no generation verb → understanding."""
    bridge, sel = _slow_bridge()
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={"timeout_seconds": 0.1})
    result = await ic.classify(
        messages=[{"role": "user", "content": "请帮我解释一下这段代码的意思"}],
        body_model=None,
    )
    assert result["generation"] == "understanding"


@pytest.mark.asyncio
async def test_heuristic_video_takes_priority_over_image():
    """When both video and image patterns could match, video wins (checked first)."""
    bridge, sel = _slow_bridge()
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={"timeout_seconds": 0.1})
    # Contains both 视频和图 — video branch checked first
    result = await ic.classify(
        messages=[{"role": "user", "content": "生成视频和图片"}],
        body_model=None,
    )
    assert result["generation"] == "video"


@pytest.mark.asyncio
async def test_heuristic_distance_limit_chinese_video():
    """Video regex requires 视频 within 20 chars of the verb."""
    bridge, sel = _slow_bridge()
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={"timeout_seconds": 0.1})
    # Verb and 视频 separated by > 20 chars → regex should NOT match.
    # But '生成视频' keyword substring still present? No — we use '制作' + padding.
    # Construct: 制作 + 25 chars + 视频 (no keyword '生成视频' substring).
    padding = "啊" * 25
    result = await ic.classify(
        messages=[{"role": "user", "content": f"制作{padding}视频"}],
        body_model=None,
    )
    # Too far apart for regex, no keyword hit → understanding
    assert result["generation"] == "understanding"
