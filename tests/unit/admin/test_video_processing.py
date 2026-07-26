"""Tests for retrieve_video 'processing' status handling."""

import asyncio
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
async def test_retrieve_video_processing():
    """Processing video should return progress without error."""
    b = _bridge()

    async def fake_get(url, headers):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "id": "video_proc",
            "status": "processing",
            "progress": 45,
            "message": "Generating video...",
        }
        resp.raise_for_status = MagicMock()
        return resp

    with patch("aigateway_core.route.bridge.litellm_bridge.httpx.AsyncClient") as MC:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.get = AsyncMock(side_effect=fake_get)
        MC.return_value = client
        result = await b.retrieve_video("video_proc")

    assert result["status"] == "processing"
    assert result["progress"] == 45
    assert result["message"] == "Generating video..."


@pytest.mark.asyncio
async def test_retrieve_video_queued():
    """Queued video should return queued status with progress."""
    b = _bridge()

    async def fake_get(url, headers):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "id": "video_queued",
            "status": "queued",
            "progress": 0,
            "message": "Video generation queued",
        }
        resp.raise_for_status = MagicMock()
        return resp

    with patch("aigateway_core.route.bridge.litellm_bridge.httpx.AsyncClient") as MC:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.get = AsyncMock(side_effect=fake_get)
        MC.return_value = client
        result = await b.retrieve_video("video_queued")

    assert result["status"] == "queued"
    assert result["progress"] == 0


@pytest.mark.asyncio
async def test_retrieve_video_no_provider_returns_error():
    """retrieve_video should return error when no provider is configured."""
    b = LiteLLMBridge(config={})
    b._build_model_list({})
    b.router = MagicMock()

    result = await b.retrieve_video("video_123")

    assert "error" in result
    assert result["error"]["code"] == "no_provider"


@pytest.mark.asyncio
async def test_retrieve_video_empty_registered_models():
    """retrieve_video should handle empty registered models gracefully."""
    b = LiteLLMBridge(config={"providers": {}})
    b._build_model_list({})
    b.router = MagicMock()

    result = await b.retrieve_video("video_123")

    assert "error" in result
    assert result["error"]["code"] == "no_provider"
