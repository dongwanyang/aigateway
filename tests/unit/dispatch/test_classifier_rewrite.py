import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.dispatch.classifier import classify_request


class _Body:
    def __init__(self, model=None, messages=None):
        self.model = model
        self.messages = messages or []


@pytest.mark.asyncio
async def test_classify_image_intent():
    ic = MagicMock()
    ic.classify = AsyncMock(return_value={"generation": "image", "hint": "None"})
    kind, hint = await classify_request(_Body(model="agnes-2.0-flash",
                                          messages=[{"role": "user", "content": "画一只猫"}]),
                                     MagicMock(), intent_classifier=ic)
    assert kind == "generation:image"
    assert hint is None


@pytest.mark.asyncio
async def test_classify_video_intent():
    ic = MagicMock()
    ic.classify = AsyncMock(return_value={"generation": "video", "hint": "None"})
    kind, hint = await classify_request(_Body(messages=[{"role": "user", "content": "生成视频"}]),
                                     MagicMock(), intent_classifier=ic)
    assert kind == "generation:video"
    assert hint is None


@pytest.mark.asyncio
async def test_classify_understanding_intent():
    ic = MagicMock()
    ic.classify = AsyncMock(return_value={"generation": "understanding", "hint": "None"})
    kind, hint = await classify_request(_Body(messages=[{"role": "user", "content": "你好"}]),
                                     MagicMock(), intent_classifier=ic)
    assert kind == "understanding"
    assert hint is None


@pytest.mark.asyncio
async def test_classification_result_carries_task_profile_without_polluting_body():
    ic = MagicMock()
    ic.classify = AsyncMock(return_value={
        "generation": "understanding",
        "hint": "None",
        "classification_source": "llm",
        "task_profile": {
            "operation": "reasoning",
            "domain": "math",
            "modalities": ["text"],
            "complexity": 90,
            "requirements": [],
            "confidence": 0.95,
        },
    })
    body = _Body(messages=[{"role": "user", "content": "证明这个定理"}])
    result = await classify_request(body, MagicMock(), intent_classifier=ic)
    kind, hint = result
    assert kind == "understanding"
    assert hint is None
    assert result.task_profile.operation == "reasoning"
    assert result.task_profile.complexity == 90
    assert not hasattr(body, "_task_profile")


@pytest.mark.asyncio
async def test_classify_no_intent_classifier_defaults_understanding():
    kind, hint = await classify_request(_Body(messages=[{"role": "user", "content": "你好"}]),
                                     MagicMock(), intent_classifier=None)
    assert kind == "understanding"
    assert hint is None


@pytest.mark.asyncio
async def test_classify_classifier_exception_defaults_understanding():
    """Classifier raising exception should fall back to understanding."""
    ic = MagicMock()
    ic.classify = AsyncMock(side_effect=RuntimeError("classifier down"))
    kind, hint = await classify_request(_Body(messages=[{"role": "user", "content": "画图"}]),
                                     MagicMock(), intent_classifier=ic)
    assert kind == "understanding"
    assert hint is None


@pytest.mark.asyncio
async def test_classify_returns_intent_hint():
    """When classifier returns a model hint, it is returned (not written to body)."""
    ic = MagicMock()
    ic.classify = AsyncMock(return_value={"generation": "image", "hint": "agnes-2.0-flash"})
    body = _Body(model="test", messages=[{"role": "user", "content": "画一只猫"}])
    kind, hint = await classify_request(body, MagicMock(), intent_classifier=ic)
    assert kind == "generation:image"
    assert hint == "agnes-2.0-flash"
    # body 不应被污染(不再 setattr _intent_hint)
    assert not hasattr(body, "_intent_hint")
