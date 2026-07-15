import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.route.bridge.litellm_bridge import LiteLLMBridge


def _bridge():
    models_config = {
        "agnes": {
            "api_key": "k",
            "base_url": "https://apihub.agnes-ai.com/v1",
            "model_grouper": [
                {"models": [{"name": "agnes-2.0-flash", "capabilities": ["text", "image"]}],
                 "fallback_models": [], "pricing": {}}
            ],
        }
    }
    b = LiteLLMBridge(config={"providers": models_config, "generation": {"image": {"default_size": "1024x1024", "response_format": "url", "quality": "auto"}}})
    b._build_model_list(models_config)
    b.router = MagicMock()
    return b


@pytest.mark.asyncio
async def test_image_endpoint_path():
    b = _bridge()
    captured = {}

    async def fake_post(url, headers, json):
        captured["url"] = url
        captured["json"] = json
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"created": 1, "data": [{"url": "https://img/x.png"}], "usage": {"input_tokens": 5, "total_tokens": 5}}
        resp.raise_for_status = MagicMock()
        return resp

    with patch("aigateway_core.route.bridge.litellm_bridge.httpx.AsyncClient") as MC:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.post = AsyncMock(side_effect=fake_post)
        MC.return_value = client
        result = await b._do_image_generation(prompt="a cat", model="agnes-2.0-flash")

    assert captured["url"].endswith("/images/generations")
    assert captured["json"]["model"] == "agnes-2.0-flash"
    assert captured["json"]["prompt"] == "a cat"
    assert captured["json"]["size"] == "1024x1024"
    assert captured["json"]["response_format"] == "url"
    # 归一为 chat completions
    assert "choices" in result
    assert "https://img/x.png" in result["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_image_response_normalized_to_chat():
    b = _bridge()

    async def fake_post(url, headers, json):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"created": 1, "data": [{"b64_json": "AAAA"}]}
        resp.raise_for_status = MagicMock()
        return resp

    with patch("aigateway_core.route.bridge.litellm_bridge.httpx.AsyncClient") as MC:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.post = AsyncMock(side_effect=fake_post)
        MC.return_value = client
        result = await b._do_image_generation(prompt="x", model="agnes-2.0-flash", response_format="b64_json")

    msg = result["choices"][0]["message"]
    assert msg["role"] == "assistant"
    assert "AAAA" in msg["content"]
    assert result["choices"][0]["finish_reason"] == "stop"
    assert "usage" in result


@pytest.mark.asyncio
async def test_image_error_response_raises():
    """Non-200 from image API should raise via raise_for_status."""
    b = _bridge()

    async def fake_post(url, headers, json):
        resp = MagicMock()
        resp.status_code = 500
        resp.raise_for_status = MagicMock(side_effect=Exception("500 Internal Server Error"))
        return resp

    with patch("aigateway_core.route.bridge.litellm_bridge.httpx.AsyncClient") as MC:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.post = AsyncMock(side_effect=fake_post)
        MC.return_value = client
        with pytest.raises(Exception, match="500"):
            await b._do_image_generation(prompt="x", model="agnes-2.0-flash")
