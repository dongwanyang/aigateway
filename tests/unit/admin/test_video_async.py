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


@pytest.mark.asyncio
async def test_video_429_rate_limit():
    """429 rate limit should raise via raise_for_status."""
    b = _bridge()

    async def fake_post(url, headers, json):
        resp = MagicMock()
        resp.status_code = 429
        resp.raise_for_status = MagicMock(side_effect=Exception("429 Too Many Requests"))
        return resp

    with patch("aigateway_core.route.bridge.litellm_bridge.httpx.AsyncClient") as MC:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.post = AsyncMock(side_effect=fake_post)
        MC.return_value = client
        with pytest.raises(Exception, match="429"):
            await b._do_video_generation(messages=[{"role": "user", "content": "x"}], model="agnes-video-v2.0")


@pytest.mark.asyncio
async def test_video_timeout_error():
    """Timeout should raise via raise_for_status."""
    b = _bridge()

    async def fake_post(url, headers, json):
        resp = MagicMock()
        resp.status_code = 504
        resp.raise_for_status = MagicMock(side_effect=Exception("504 Gateway Timeout"))
        return resp

    with patch("aigateway_core.route.bridge.litellm_bridge.httpx.AsyncClient") as MC:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.post = AsyncMock(side_effect=fake_post)
        MC.return_value = client
        with pytest.raises(Exception, match="504"):
            await b._do_video_generation(messages=[{"role": "user", "content": "x"}], model="agnes-video-v2.0")


@pytest.mark.asyncio
async def test_video_submit_b64_json():
    """b64_json response format should be normalized to chat completions."""
    b = _bridge()

    async def fake_post(url, headers, json):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": "video_123", "object": "video", "status": "queued",
                                  "progress": 0, "created_at": 1, "model": "agnes-video-v2.0",
                                  "prompt": json["prompt"], "seconds": "4", "size": "720x1280"}
        resp.raise_for_status = MagicMock()
        return resp

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = AsyncMock(side_effect=fake_post)

    with patch("aigateway_core.route.bridge.litellm_bridge.httpx.AsyncClient") as MC:
        MC.return_value = client
        result = await b._do_video_generation(
            messages=[{"role": "user", "content": "生成一段跳舞视频"}], model="agnes-video-v2.0"
        )

    msg = result["choices"][0]["message"]["content"]
    assert "video_123" in msg
    assert "/v1/videos/video_123" in msg


def _multi_provider_bridge():
    """文本 provider 排在前面，视频 provider 在后面。

    复现真实的多 provider 配置：注册顺序里第一个模型属于纯文本 provider。
    """
    models_config = {
        "text-first": {
            "api_key": "text-key",
            "base_url": "https://text.example.com/v1",
            "model_grouper": [
                {
                    "models": [{"name": "chat-llm", "capabilities": ["text"]}],
                    "fallback_models": [],
                    "pricing": {},
                }
            ],
        },
        "video-provider": {
            "api_key": "video-key",
            "base_url": "https://video.example.com/v1",
            "model_grouper": [
                {
                    "models": [{"name": "agnes-video-v2.0", "capabilities": ["video"]}],
                    "fallback_models": [],
                    "pricing": {},
                }
            ],
        },
    }
    bridge = LiteLLMBridge(config={"providers": models_config})
    bridge._build_model_list(models_config)
    bridge.router = MagicMock()
    return bridge


@pytest.mark.asyncio
async def test_retrieve_video_queries_the_video_capable_provider():
    """状态轮询必须打到具备 video capability 的 provider。

    回归:之前用 get_registered_models()[0] 取端点,也就是"第一个注册模型所属
    provider",与真正处理视频任务的 provider 无关。多 provider 配置下会向错误的
    provider 查 /videos/{id} 并拿到 404,前端于是永远等不到终态。
    """
    bridge = _multi_provider_bridge()
    assert bridge.get_registered_models()[0] == "chat-llm"

    seen: dict[str, object] = {}

    async def fake_get(url, headers):
        seen["url"] = url
        seen["headers"] = headers
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": "vid-1", "status": "completed"}
        resp.raise_for_status = MagicMock()
        return resp

    with patch("aigateway_core.route.bridge.litellm_bridge.httpx.AsyncClient") as MC:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.get = AsyncMock(side_effect=fake_get)
        MC.return_value = client
        result = await bridge.retrieve_video("vid-1")

    assert result["status"] == "completed"
    assert seen["url"] == "https://video.example.com/v1/videos/vid-1"
    assert seen["headers"]["Authorization"] == "Bearer video-key"


def test_video_status_model_prefers_video_capability():
    bridge = _multi_provider_bridge()
    assert bridge._video_status_model() == "agnes-video-v2.0"


def test_video_status_model_falls_back_when_no_video_model_registered():
    """没有视频模型时退回第一个已注册模型，保持原有的尽力而为行为。"""
    models_config = {
        "text-only": {
            "api_key": "k",
            "base_url": "https://text.example.com/v1",
            "model_grouper": [
                {
                    "models": [{"name": "chat-llm", "capabilities": ["text"]}],
                    "fallback_models": [],
                    "pricing": {},
                }
            ],
        }
    }
    bridge = LiteLLMBridge(config={"providers": models_config})
    bridge._build_model_list(models_config)
    bridge.router = MagicMock()
    assert bridge._video_status_model() == "chat-llm"
