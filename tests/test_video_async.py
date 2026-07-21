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
                {"models": [{"name": "agnes-video-v2.0", "capabilities": ["video"]}],
                 "fallback_models": [], "pricing": {}}
            ],
        }
    }
    b = LiteLLMBridge(config={"providers": models_config})
    b._build_model_list(models_config)
    b.router = MagicMock()
    return b


@pytest.mark.asyncio
async def test_video_submit_returns_task_id():
    b = _bridge()

    async def fake_post(url, headers, json):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": "video_123", "object": "video", "status": "queued",
                                  "progress": 0, "created_at": 1, "model": "agnes-video-v2.0",
                                  "prompt": json["prompt"], "seconds": "4", "size": "720x1280"}
        resp.raise_for_status = MagicMock()
        return resp

    with patch("aigateway_core.route.bridge.litellm_bridge.httpx.AsyncClient") as MC:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.post = AsyncMock(side_effect=fake_post)
        MC.return_value = client
        result = await b._do_video_generation(
            messages=[{"role": "user", "content": "生成一段跳舞视频"}], model="agnes-video-v2.0"
        )

    msg = result["choices"][0]["message"]["content"]
    # Verify structured response: task_id and poll endpoint referenced
    assert "video_123" in msg
    assert "/v1/videos/video_123" in msg


@pytest.mark.asyncio
async def test_retrieve_video_completed():
    """Completed video should return final URL."""
    b = _bridge()

    async def fake_get(url, headers):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": "video_done", "status": "completed", "progress": 100,
                                  "url": "https://cdn.example.com/video.mp4"}
        resp.raise_for_status = MagicMock()
        return resp

    with patch("aigateway_core.route.bridge.litellm_bridge.httpx.AsyncClient") as MC:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.get = AsyncMock(side_effect=fake_get)
        MC.return_value = client
        result = await b.retrieve_video("video_done")

    assert result["status"] == "completed"
    assert result["url"] == "https://cdn.example.com/video.mp4"


@pytest.mark.asyncio
async def test_retrieve_video_failed():
    """Failed video should report error status."""
    b = _bridge()

    async def fake_get(url, headers):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": "video_fail", "status": "failed", "error": "timeout"}
        resp.raise_for_status = MagicMock()
        return resp

    with patch("aigateway_core.route.bridge.litellm_bridge.httpx.AsyncClient") as MC:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.get = AsyncMock(side_effect=fake_get)
        MC.return_value = client
        result = await b.retrieve_video("video_fail")

    assert result["status"] == "failed"
    assert result["error"] == "timeout"


@pytest.mark.asyncio
async def test_video_submit_endpoint_path():
    b = _bridge()
    captured = {}

    async def fake_post(url, headers, json):
        captured["url"] = url
        captured["json"] = json
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": "video_1", "object": "video", "status": "queued", "progress": 0}
        resp.raise_for_status = MagicMock()
        return resp

    with patch("aigateway_core.route.bridge.litellm_bridge.httpx.AsyncClient") as MC:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.post = AsyncMock(side_effect=fake_post)
        MC.return_value = client
        await b._do_video_generation(messages=[{"role": "user", "content": "x"}], model="agnes-video-v2.0")

    assert captured["url"].endswith("/videos")
    assert captured["json"]["prompt"] == "x"
    assert captured["json"]["model"] == "agnes-video-v2.0"


@pytest.mark.asyncio
async def test_retrieve_video_polls_status():
    b = _bridge()

    async def fake_get(url, headers):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": "video_123", "status": "in_progress", "progress": 50}
        resp.raise_for_status = MagicMock()
        return resp

    with patch("aigateway_core.route.bridge.litellm_bridge.httpx.AsyncClient") as MC:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.get = AsyncMock(side_effect=fake_get)
        MC.return_value = client
        result = await b.retrieve_video("video_123")

    assert result["status"] == "in_progress"
    assert result["progress"] == 50
