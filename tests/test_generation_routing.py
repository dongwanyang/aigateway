"""End-to-end generation routing: hint priority, capabilities pool filtering."""
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

from aigateway_core.route.bridge.litellm_bridge import LiteLLMBridge


def _bridge(models_config=None):
    """Build a LiteLLMBridge with capabilities-based model config."""
    if models_config is None:
        models_config = {
            "agnes": {
                "api_key": "k", "base_url": "https://apihub.agnes-ai.com/v1",
                "model_grouper": [{
                    "models": [
                        {"name": "agnes-2.0-flash", "capabilities": ["text", "image", "video"]},
                        {"name": "agnes-image-2.1-flash", "capabilities": ["image"]},
                        {"name": "deepseek-v4-flash", "capabilities": ["text"]},
                    ], "fallback_models": [], "pricing": {},
                }],
            }
        }
    b = LiteLLMBridge(config={"providers": models_config})
    b._build_model_list(models_config)
    b.router = MagicMock()
    return b


@pytest.mark.asyncio
async def test_hint_in_pool_preferred_over_body_model():
    """Hint in pool takes priority over body.model."""
    b = _bridge()
    resolved = await b._resolve_by_intent(intent="generation:image", model_hint="agnes-image-2.1-flash")
    assert resolved["model"] == "agnes-image-2.1-flash"
    assert resolved["meta"]["reason"] == "hint_matched"
    assert resolved["meta"]["intent"] == "generation:image"


@pytest.mark.asyncio
async def test_hint_not_in_pool_ignored():
    """Hint is a text-only model but intent is image -> hint ignored, pool model selected."""
    b = _bridge()
    resolved = await b._resolve_by_intent(intent="generation:image", model_hint="deepseek-v4-flash")
    # pool_first: agnes-2.0-flash is first in registration order with image capability
    assert resolved["model"] == "agnes-2.0-flash"
    assert resolved["meta"]["reason"] == "pool_first"


@pytest.mark.asyncio
async def test_no_hint_picks_image_pool():
    """No hint -> picks first model in pool with image capability."""
    b = _bridge()
    resolved = await b._resolve_by_intent(intent="generation:image", model_hint=None)
    # pool_first: agnes-2.0-flash is first in registration order with image capability
    assert resolved["model"] == "agnes-2.0-flash"
    assert resolved["meta"]["reason"] == "pool_first"


@pytest.mark.asyncio
async def test_understanding_pools_text_models():
    """Understanding intent -> pools text-capable models."""
    b = _bridge()
    resolved = await b._resolve_by_intent(intent="understanding", model_hint=None)
    # pool_first: agnes-2.0-flash is first with text capability
    assert resolved["model"] == "agnes-2.0-flash"
    assert resolved["meta"]["intent"] == "understanding"


@pytest.mark.asyncio
async def test_video_intent_pools_video_models():
    """Video intent -> pools video-capable models."""
    b = _bridge()
    resolved = await b._resolve_by_intent(intent="generation:video", model_hint=None)
    assert resolved["model"] == "agnes-2.0-flash"  # only model with video capability
    assert resolved["meta"]["intent"] == "generation:video"


@pytest.mark.asyncio
async def test_polymorphic_model_selected_for_both_text_and_image():
    """agnes-2.0-flash is polymorphic: selected for both understanding and image."""
    b = _bridge()
    r1 = await b._resolve_by_intent(intent="understanding", model_hint="agnes-2.0-flash")
    r2 = await b._resolve_by_intent(intent="generation:image", model_hint="agnes-2.0-flash")
    assert r1["model"] == "agnes-2.0-flash"
    assert r2["model"] == "agnes-2.0-flash"
    assert r1["meta"]["reason"] == "hint_matched"
    assert r2["meta"]["reason"] == "hint_matched"


@pytest.mark.asyncio
async def test_empty_pool_returns_error():
    """No video-capable models -> returns structured error."""
    models_config = {
        "agnes": {
            "api_key": "k", "base_url": "https://apihub.agnes-ai.com/v1",
            "model_grouper": [{
                "models": [
                    {"name": "agnes-2.0-flash", "capabilities": ["text", "image"]},
                    {"name": "agnes-image-2.1-flash", "capabilities": ["image"]},
                ], "fallback_models": [], "pricing": {},
            }],
        }
    }
    b = _bridge(models_config)
    resolved = await b._resolve_by_intent(intent="generation:video", model_hint=None)
    assert "error" in resolved
    assert resolved["error"]["code"] == "no_model_for_intent"
