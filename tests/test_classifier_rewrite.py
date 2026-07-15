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
    result = await classify_request(_Body(model="agnes-2.0-flash",
                                          messages=[{"role": "user", "content": "画一只猫"}]),
                                     MagicMock(), intent_classifier=ic)
    assert result == "generation:image"


@pytest.mark.asyncio
async def test_classify_video_intent():
    ic = MagicMock()
    ic.classify = AsyncMock(return_value={"generation": "video", "hint": "None"})
    result = await classify_request(_Body(messages=[{"role": "user", "content": "生成视频"}]),
                                     MagicMock(), intent_classifier=ic)
    assert result == "generation:video"


@pytest.mark.asyncio
async def test_classify_understanding_intent():
    ic = MagicMock()
    ic.classify = AsyncMock(return_value={"generation": "understanding", "hint": "None"})
    result = await classify_request(_Body(messages=[{"role": "user", "content": "你好"}]),
                                     MagicMock(), intent_classifier=ic)
    assert result == "understanding"


@pytest.mark.asyncio
async def test_classify_no_intent_classifier_defaults_understanding():
    result = await classify_request(_Body(messages=[{"role": "user", "content": "你好"}]),
                                     MagicMock(), intent_classifier=None)
    assert result == "understanding"


@pytest.mark.asyncio
async def test_classify_classifier_exception_defaults_understanding():
    """Classifier raising exception should fall back to understanding."""
    ic = MagicMock()
    ic.classify = AsyncMock(side_effect=RuntimeError("classifier down"))
    result = await classify_request(_Body(messages=[{"role": "user", "content": "画图"}]),
                                     MagicMock(), intent_classifier=ic)
    assert result == "understanding"


@pytest.mark.asyncio
async def test_classify_sets_intent_hint_on_body():
    """When classifier returns valid result, body._intent_hint should be set."""
    ic = MagicMock()
    ic.classify = AsyncMock(return_value={"generation": "image", "hint": "agnes-2.0-flash"})
    body = _Body(model="test", messages=[{"role": "user", "content": "画一只猫"}])
    result = await classify_request(body, MagicMock(), intent_classifier=ic)
    assert result == "generation:image"
    assert hasattr(body, "_intent_hint")
    assert body._intent_hint == "agnes-2.0-flash"
