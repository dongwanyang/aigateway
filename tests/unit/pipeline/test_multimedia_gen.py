"""Tests for benchmarks/scenarios/multimedia_gen.py — T4: Multimedia adapter with error handling."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from benchmarks.scenarios.multimedia_gen import generate_image, generate_video


def _make_session(post_resp=None, get_resp=None):
    """Create a mock aiohttp.ClientSession with properly chained async context managers."""
    post_ctx = MagicMock()
    post_ctx.__aenter__.return_value = post_resp or MagicMock()
    post_ctx.__aexit__.return_value = False

    get_ctx = MagicMock()
    get_ctx.__aenter__.return_value = get_resp or MagicMock()
    get_ctx.__aexit__.return_value = False

    session = MagicMock()
    session.post.return_value = post_ctx
    session.get.return_value = get_ctx
    return session


class TestGenerateImage:
    @pytest.mark.asyncio
    async def test_success(self):
        resp = MagicMock()
        resp.status = 200
        resp.json = AsyncMock(return_value={"data": [{"url": "https://example.com/image.png"}]})

        session = _make_session(post_resp=resp)

        result = await generate_image(
            session=session,
            base_url="http://localhost:8000",
            headers={"Authorization": "Bearer test"},
            prompt="test image",
            model="agnes-image-2.1-flash",
        )

        assert result["ok"] is True
        assert result["url"] == "https://example.com/image.png"
        assert result["media_type"] == "image"

    @pytest.mark.asyncio
    async def test_http_error(self):
        resp = MagicMock()
        resp.status = 400
        resp.text = AsyncMock(return_value="invalid model")

        session = _make_session(post_resp=resp)

        result = await generate_image(
            session=session,
            base_url="http://localhost:8000",
            headers={"Authorization": "Bearer test"},
            prompt="test image",
            model="bad-model",
        )

        assert result["ok"] is False
        assert "HTTP 400" in result["error"]

    @pytest.mark.asyncio
    async def test_exception(self):
        session = _make_session(post_resp=MagicMock())
        session.post.side_effect = ConnectionError("network down")

        result = await generate_image(
            session=session,
            base_url="http://localhost:8000",
            headers={"Authorization": "Bearer test"},
            prompt="test image",
            model="agnes-image-2.1-flash",
        )

        assert result["ok"] is False
        assert "network down" in result["error"]


class TestGenerateVideo:
    @pytest.mark.asyncio
    async def test_success_after_polling(self):
        submit_resp = MagicMock()
        submit_resp.status = 200
        submit_resp.json = AsyncMock(return_value={"task_id": "vid-123"})

        poll_resp = MagicMock()
        poll_resp.status = 200
        poll_resp.json = AsyncMock(return_value={"status": "completed"})

        # First call: post -> submit_resp; Second call: get -> poll_resp
        call_count = {"n": 0}
        responses = [submit_resp, poll_resp]

        def side_effect(*args, **kwargs):
            r = responses[call_count["n"]]
            call_count["n"] += 1
            return r

        post_ctx = MagicMock()
        post_ctx.__aenter__.return_value = submit_resp
        post_ctx.__aexit__.return_value = False

        get_ctx = MagicMock()
        get_ctx.__aenter__.return_value = poll_resp
        get_ctx.__aexit__.return_value = False

        session = MagicMock()
        session.post.return_value = post_ctx
        session.get.return_value = get_ctx

        result = await generate_video(
            session=session,
            base_url="http://localhost:8000",
            headers={"Authorization": "Bearer test"},
            prompt="test video",
            model="agnes-video-2.1",
            max_attempts=5,
            poll_interval=0.1,
        )

        assert result["ok"] is True
        assert result["task_id"] == "vid-123"
        assert result["final_status"] == "completed"
        assert result["media_type"] == "video"

    @pytest.mark.asyncio
    async def test_timeout(self):
        submit_resp = MagicMock()
        submit_resp.status = 200
        submit_resp.json = AsyncMock(return_value={"task_id": "vid-timeout"})

        poll_resp = MagicMock()
        poll_resp.status = 200
        poll_resp.json = AsyncMock(return_value={"status": "processing"})

        post_ctx = MagicMock()
        post_ctx.__aenter__.return_value = submit_resp
        post_ctx.__aexit__.return_value = False

        get_ctx = MagicMock()
        get_ctx.__aenter__.return_value = poll_resp
        get_ctx.__aexit__.return_value = False

        session = MagicMock()
        session.post.return_value = post_ctx
        session.get.return_value = get_ctx

        result = await generate_video(
            session=session,
            base_url="http://localhost:8000",
            headers={"Authorization": "Bearer test"},
            prompt="slow video",
            model="agnes-video-2.1",
            max_attempts=3,
            poll_interval=0.1,
        )

        assert result["ok"] is False
        assert result["status"] == "video_timeout"
        assert "timed out" in result["error"]

    @pytest.mark.asyncio
    async def test_submit_error(self):
        submit_resp = MagicMock()
        submit_resp.status = 500
        submit_resp.text = AsyncMock(return_value="server error")

        post_ctx = MagicMock()
        post_ctx.__aenter__.return_value = submit_resp
        post_ctx.__aexit__.return_value = False

        session = MagicMock()
        session.post.return_value = post_ctx

        result = await generate_video(
            session=session,
            base_url="http://localhost:8000",
            headers={"Authorization": "Bearer test"},
            prompt="test video",
            model="agnes-video-2.1",
        )

        assert result["ok"] is False
        assert result["status"] == "video_submit_error"
        assert "HTTP 500" in result["error"]

    @pytest.mark.asyncio
    async def test_failed_during_polling(self):
        submit_resp = MagicMock()
        submit_resp.status = 200
        submit_resp.json = AsyncMock(return_value={"task_id": "vid-failed"})

        poll_resp = MagicMock()
        poll_resp.status = 200
        poll_resp.json = AsyncMock(return_value={"status": "failed", "error": "generation failed"})

        post_ctx = MagicMock()
        post_ctx.__aenter__.return_value = submit_resp
        post_ctx.__aexit__.return_value = False

        get_ctx = MagicMock()
        get_ctx.__aenter__.return_value = poll_resp
        get_ctx.__aexit__.return_value = False

        session = MagicMock()
        session.post.return_value = post_ctx
        session.get.return_value = get_ctx

        result = await generate_video(
            session=session,
            base_url="http://localhost:8000",
            headers={"Authorization": "Bearer test"},
            prompt="test video",
            model="agnes-video-2.1",
        )

        assert result["ok"] is False
        assert result["status"] == "failed"
