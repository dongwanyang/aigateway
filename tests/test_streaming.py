"""Unit tests for streaming.py — create_sse_response.

Verifies:
- StreamingResponse has correct media_type
- SSE headers are set (Cache-Control, Connection, X-Accel-Buffering)
- SSEGenerator is wrapped correctly
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

import asyncio
import pytest
from unittest.mock import MagicMock, patch
from fastapi.responses import StreamingResponse


async def _collect_stream_chunks(body_iterator):
    chunks = []
    async for chunk in body_iterator:
        chunks.append(chunk)
    return chunks


class TestCreateSseResponse:
    """Test create_sse_response wrapper function."""

    def _make_async_iterator(self, items=None):
        """Create an async iterator that yields dicts."""
        items = items or [{"content": "hello"}, {"content": "world"}]

        async def gen():
            for item in items:
                yield item

        return gen()

    @patch("aigateway_api.streaming.SSEGenerator")
    def test_returns_streaming_response(self, mock_sse_gen):
        from aigateway_api.streaming import create_sse_response

        async def _fake_sse_stream():
            yield b"data: test\n\n"

        mock_sse_gen_instance = MagicMock()
        mock_sse_gen_instance.generate.return_value = _fake_sse_stream()
        mock_sse_gen.return_value = mock_sse_gen_instance

        completion_gen = self._make_async_iterator()
        response = create_sse_response(completion_gen, chat_id="test-chat-1")

        assert isinstance(response, StreamingResponse)

    @patch("aigateway_api.streaming.SSEGenerator")
    def test_media_type_is_event_stream(self, mock_sse_gen):
        from aigateway_api.streaming import create_sse_response

        mock_sse_gen.return_value = MagicMock()
        completion_gen = self._make_async_iterator()
        response = create_sse_response(completion_gen)

        assert response.media_type == "text/event-stream"

    @patch("aigateway_api.streaming.SSEGenerator")
    def test_cache_control_no_cache(self, mock_sse_gen):
        from aigateway_api.streaming import create_sse_response

        mock_sse_gen.return_value = MagicMock()
        completion_gen = self._make_async_iterator()
        response = create_sse_response(completion_gen)

        assert response.headers.get("cache-control") == "no-cache"

    @patch("aigateway_api.streaming.SSEGenerator")
    def test_connection_keep_alive(self, mock_sse_gen):
        from aigateway_api.streaming import create_sse_response

        mock_sse_gen.return_value = MagicMock()
        completion_gen = self._make_async_iterator()
        response = create_sse_response(completion_gen)

        assert response.headers.get("connection") == "keep-alive"

    @patch("aigateway_api.streaming.SSEGenerator")
    def test_accel_buffering_disabled(self, mock_sse_gen):
        from aigateway_api.streaming import create_sse_response

        mock_sse_gen.return_value = MagicMock()
        completion_gen = self._make_async_iterator()
        response = create_sse_response(completion_gen)

        assert response.headers.get("x-accel-buffering") == "no"

    @patch("aigateway_api.streaming.SSEGenerator")
    def test_chat_id_passed_to_generator(self, mock_sse_gen):
        from aigateway_api.streaming import create_sse_response

        mock_sse_gen.return_value = MagicMock()
        completion_gen = self._make_async_iterator()
        create_sse_response(completion_gen, chat_id="chat-abc-123")

        mock_sse_gen.assert_called_once()
        call_args = mock_sse_gen.call_args
        # First arg should be the completion_gen, second should be chat_id
        assert call_args[0][1] == "chat-abc-123"

    @patch("aigateway_api.streaming.SSEGenerator")
    def test_no_chat_id_works(self, mock_sse_gen):
        from aigateway_api.streaming import create_sse_response

        mock_sse_gen.return_value = MagicMock()
        completion_gen = self._make_async_iterator()
        response = create_sse_response(completion_gen, chat_id=None)

        assert isinstance(response, StreamingResponse)
        mock_sse_gen.assert_called_once()
        call_args = mock_sse_gen.call_args
        assert call_args[0][1] is None

    @patch("aigateway_api.streaming.SSEGenerator")
    def test_all_headers_present(self, mock_sse_gen):
        """Verify all three expected headers are set."""
        from aigateway_api.streaming import create_sse_response

        mock_sse_gen.return_value = MagicMock()
        completion_gen = self._make_async_iterator()
        response = create_sse_response(completion_gen)

        expected_headers = {
            "cache-control": "no-cache",
            "connection": "keep-alive",
            "x-accel-buffering": "no",
        }
        for header_name, expected_value in expected_headers.items():
            actual = response.headers.get(header_name)
            assert actual == expected_value, f"Header {header_name}: expected '{expected_value}', got '{actual}'"

    def test_streaming_response_body_iterator_emits_sse_bytes(self):
        from aigateway_api.streaming import create_sse_response

        async def _fake_generate(self):
            yield b"data: {\"delta\":\"hello\"}\n\n"
            yield b"data: [DONE]\n\n"

        with patch("aigateway_api.streaming.SSEGenerator.generate", _fake_generate):
            response = create_sse_response(self._make_async_iterator(), chat_id="chat-1")
            chunks = asyncio.new_event_loop().run_until_complete(
                _collect_stream_chunks(response.body_iterator)
            )

        assert chunks == [
            b"data: {\"delta\":\"hello\"}\n\n",
            b"data: [DONE]\n\n",
        ]
