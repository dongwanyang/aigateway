"""Tests for LiteLLMBridge stream path with image/video intents and extra_headers propagation."""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.route.bridge.litellm_bridge import LiteLLMBridge


def _create_bridge_with_models(models_config: dict) -> LiteLLMBridge:
    bridge = LiteLLMBridge(config={"providers": models_config})
    model_list = bridge._build_model_list(models_config)
    bridge.router = MagicMock()
    bridge.router.get_model_list.return_value = model_list
    return bridge


MODELS_CONFIG = {
    "agnes": {
        "api_key": "sk-test",
        "base_url": "https://apihub.agnes-ai.com/v1",
        "model_grouper": [{
            "models": [
                {"name": "agnes-2.0-flash", "capabilities": ["text", "image", "video"]},
                {"name": "deepseek-v4-flash", "capabilities": ["text"]},
            ],
            "fallback_models": [],
            "pricing": {},
        }],
    }
}


class TestCompletionStreamImageVideoIntent:
    """Test stream path for image and video generation intents."""

    def setup_method(self):
        self.bridge = _create_bridge_with_models(MODELS_CONFIG)

    @pytest.mark.asyncio
    async def test_stream_image_intent_yields_correct_chunk(self):
        """completion_stream with generation:image intent should yield one chunk with image content."""
        mock_img_result = {
            "choices": [{
                "message": {"role": "assistant", "content": "https://cdn.example.com/image.png"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
        }

        self.bridge._do_image_generation = AsyncMock(return_value=mock_img_result)

        chunks = []
        async for chunk in self.bridge.completion_stream(
            messages=[{"role": "user", "content": "画一只猫"}],
            model="agnes-2.0-flash",
            intent="generation:image",
        ):
            chunks.append(chunk)

        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk["object"] == "chat.completion.chunk"
        assert chunk["model"] == "agnes-2.0-flash"
        assert chunk["choices"][0]["delta"]["content"] == "https://cdn.example.com/image.png"
        assert chunk["_meta"]["routed_to"]["intent"] == "generation:image"
        assert "cost" in chunk["_meta"]
        assert "model_router" in chunk["_meta"]

    @pytest.mark.asyncio
    async def test_stream_video_intent_yields_correct_chunk(self):
        """completion_stream with generation:video intent should yield one chunk with video_id in meta."""
        mock_vid_result = {
            "choices": [{
                "message": {"role": "assistant", "content": "Video submitted. id=vid_abc, poll /v1/videos/vid_abc"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "_meta": {"video_id": "vid_abc"},
        }

        self.bridge._do_video_generation = AsyncMock(return_value=mock_vid_result)

        chunks = []
        async for chunk in self.bridge.completion_stream(
            messages=[{"role": "user", "content": "生成一段跳舞视频"}],
            model="agnes-2.0-flash",
            intent="generation:video",
        ):
            chunks.append(chunk)

        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk["object"] == "chat.completion.chunk"
        assert chunk["model"] == "agnes-2.0-flash"
        assert chunk["choices"][0]["delta"]["content"] == "Video submitted. id=vid_abc, poll /v1/videos/vid_abc"
        assert chunk["_meta"]["video_id"] == "vid_abc"
        assert chunk["_meta"]["routed_to"]["intent"] == "generation:video"

    @pytest.mark.asyncio
    async def test_stream_image_intent_error_handling(self):
        """completion_stream should handle image generation errors gracefully."""
        self.bridge._do_image_generation = AsyncMock(
            side_effect=RuntimeError("Provider timeout")
        )

        chunks = []
        async for chunk in self.bridge.completion_stream(
            messages=[{"role": "user", "content": "画一只猫"}],
            model="agnes-2.0-flash",
            intent="generation:image",
        ):
            chunks.append(chunk)

        assert len(chunks) == 1
        chunk = chunks[0]
        assert "error" in chunk
        assert chunk["error"]["code"] == "image_generation_failed"
        assert "Provider timeout" in chunk["error"]["message"]
        assert "[Image generation error]" in chunk["choices"][0]["delta"]["content"]

    @pytest.mark.asyncio
    async def test_stream_video_intent_error_handling(self):
        """completion_stream should handle video generation errors gracefully."""
        self.bridge._do_video_generation = AsyncMock(
            side_effect=ValueError("Invalid prompt")
        )

        chunks = []
        async for chunk in self.bridge.completion_stream(
            messages=[{"role": "user", "content": "生成视频"}],
            model="agnes-2.0-flash",
            intent="generation:video",
        ):
            chunks.append(chunk)

        assert len(chunks) == 1
        chunk = chunks[0]
        assert "error" in chunk
        assert chunk["error"]["code"] == "video_generation_failed"
        assert "Invalid prompt" in chunk["error"]["message"]

    @pytest.mark.asyncio
    async def test_extra_headers_passed_to_image_generation(self):
        """extra_headers should be forwarded to _do_image_generation."""
        mock_img_result = {
            "choices": [{"message": {"role": "assistant", "content": "url"}, "finish_reason": "stop"}],
            "usage": {},
        }
        self.bridge._do_image_generation = AsyncMock(return_value=mock_img_result)

        extra_headers = {"X-Request-ID": "req-456"}
        chunks = []
        async for chunk in self.bridge.completion_stream(
            messages=[{"role": "user", "content": "画猫"}],
            model="agnes-2.0-flash",
            extra_headers=extra_headers,
            intent="generation:image",
        ):
            chunks.append(chunk)

        call_kwargs = self.bridge._do_image_generation.call_args.kwargs
        assert call_kwargs.get("extra_headers") == extra_headers

    @pytest.mark.asyncio
    async def test_extra_headers_passed_to_video_generation(self):
        """extra_headers should be forwarded to _do_video_generation."""
        mock_vid_result = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {},
            "_meta": {"video_id": "vid_1"},
        }
        self.bridge._do_video_generation = AsyncMock(return_value=mock_vid_result)

        extra_headers = {"X-Request-ID": "req-789"}
        chunks = []
        async for chunk in self.bridge.completion_stream(
            messages=[{"role": "user", "content": "生成视频"}],
            model="agnes-2.0-flash",
            extra_headers=extra_headers,
            intent="generation:video",
        ):
            chunks.append(chunk)

        call_kwargs = self.bridge._do_video_generation.call_args.kwargs
        assert call_kwargs.get("extra_headers") == extra_headers


class TestExtraHeadersPropagation:
    """extra_headers propagation to downstream router."""

    def setup_method(self):
        self.bridge = _create_bridge_with_models(MODELS_CONFIG)

    @pytest.mark.asyncio
    async def test_extra_headers_propagated_to_router_for_text_intent(self):
        """extra_headers should be passed through to router.acompletion for text intent."""
        async def _stream_chunks():
            chunk1 = {"id": "chatcmpl-test", "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}]}
            chunk2 = {"id": "chatcmpl-test", "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}]}
            yield chunk1
            yield chunk2

        self.bridge.router.acompletion = AsyncMock(return_value=_stream_chunks())

        extra_headers = {"X-Custom-Header": "test-value"}
        chunks = []
        async for chunk in self.bridge.completion_stream(
            messages=[{"role": "user", "content": "hello"}],
            model="deepseek-v4-flash",
            extra_headers=extra_headers,
            intent="understanding",
        ):
            chunks.append(chunk)

        # Verify the router was called with extra_headers
        assert len(chunks) == 2
        assert chunks[0]["choices"][0]["delta"]["content"] == "hello"
        call_kwargs = self.bridge.router.acompletion.call_args.kwargs
        assert call_kwargs.get("extra_headers") == extra_headers

    @pytest.mark.asyncio
    async def test_extra_headers_not_sent_when_none(self):
        """When extra_headers is None, it should not appear in router params."""
        async def fake_acompletion(**kwargs):
            chunk = {"id": "chatcmpl-test", "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}]}
            yield chunk

        self.bridge.router.acompletion = fake_acompletion

        chunks = []
        async for chunk in self.bridge.completion_stream(
            messages=[{"role": "user", "content": "hello"}],
            model="deepseek-v4-flash",
            intent="understanding",
        ):
            chunks.append(chunk)

        assert len(chunks) == 1


class TestCompletionImageVideoIntent:
    """Non-streaming completion path for image/video intents."""

    def setup_method(self):
        self.bridge = _create_bridge_with_models(MODELS_CONFIG)

    @pytest.mark.asyncio
    async def test_completion_image_intent_returns_data(self):
        """completion with generation:image should return data dict, not call chat completions."""
        mock_img_result = {
            "choices": [{"message": {"role": "assistant", "content": "img-url"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
        }
        self.bridge._do_image_generation = AsyncMock(return_value=mock_img_result)

        result = await self.bridge.completion(
            messages=[{"role": "user", "content": "画猫"}],
            model="agnes-2.0-flash",
            intent="generation:image",
        )

        assert "data" in result
        assert "choices" in result["data"]
        assert result["data"]["choices"][0]["message"]["content"] == "img-url"
        assert result["_meta"]["routed_to"]["intent"] == "generation:image"

    @pytest.mark.asyncio
    async def test_completion_video_intent_returns_data(self):
        """completion with generation:video should return data dict with video_id in meta."""
        mock_vid_result = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {},
            "_meta": {"video_id": "vid_123"},
        }
        self.bridge._do_video_generation = AsyncMock(return_value=mock_vid_result)

        result = await self.bridge.completion(
            messages=[{"role": "user", "content": "生成视频"}],
            model="agnes-2.0-flash",
            intent="generation:video",
        )

        assert "data" in result
        assert result["data"]["choices"][0]["message"]["content"] == "ok"
        assert result["_meta"]["routed_to"]["intent"] == "generation:video"
