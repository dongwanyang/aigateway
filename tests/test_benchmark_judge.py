"""Tests for benchmarks/judge.py — T3: LLM-as-judge with error handling."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from benchmarks.judge import _parse_judge_response, run_judge


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class TestParseJudgeResponse:
    def test_valid_json(self):
        content = '{"response_a_score": 4, "response_b_score": 5}'
        result = _parse_judge_response(content)
        assert result == 4.5

    def test_clamps_scores(self):
        content = '{"response_a_score": 0, "response_b_score": 10}'
        result = _parse_judge_response(content)
        # Each score clamped to [1,5]: 0→1, 10→5, average=3.0
        assert result == 3.0

    def test_rejects_missing_keys(self):
        content = '{"score": 4}'
        assert _parse_judge_response(content) is None

    def test_rejects_non_numeric(self):
        content = '{"response_a_score": "high", "response_b_score": 4}'
        assert _parse_judge_response(content) is None

    def test_markdown_code_block(self):
        content = '```json\n{"response_a_score": 3, "response_b_score": 4}\n```'
        result = _parse_judge_response(content)
        assert result == 3.5


# ---------------------------------------------------------------------------
# run_judge integration
# ---------------------------------------------------------------------------

def _make_mock_session(resp_status: int, resp_body: dict) -> MagicMock:
    """Create a mock aiohttp.ClientSession with proper async context managers."""
    mock_resp = MagicMock()
    mock_resp.status = resp_status
    mock_resp.json = AsyncMock(return_value=resp_body)
    if resp_status >= 400:
        mock_resp.text = AsyncMock(return_value="error")

    mock_session = MagicMock()
    post_ctx = AsyncMock()
    post_ctx.__aenter__.return_value = mock_resp
    post_ctx.__aexit__.return_value = False
    mock_session.post.return_value = post_ctx

    return mock_session


class TestRunJudge:
    @pytest.mark.asyncio
    async def test_run_judge_returns_average(self):
        """Two successful judge calls return averaged score."""
        mock_session = _make_mock_session(200, {
            "choices": [{"message": {"content": '{"response_a_score": 4, "response_b_score": 5}'}}]
        })

        result = await run_judge(
            prompt="test",
            response_a="A",
            response_b="B",
            judge_api_url="http://fake/judge",
            judge_api_key="key",
            judge_model="deepseek-v4-flash",
            session=mock_session,
        )

        assert result == pytest.approx(4.5)

    @pytest.mark.asyncio
    async def test_run_judge_handles_timeout(self):
        """API timeout → None, sample.status becomes judge_error."""
        mock_session = MagicMock()
        post_ctx = AsyncMock()
        post_ctx.__aenter__.side_effect = asyncio.TimeoutError("timeout")
        post_ctx.__aexit__.return_value = False
        mock_session.post.return_value = post_ctx

        result = await run_judge(
            prompt="test",
            response_a="A",
            response_b="B",
            judge_api_url="http://fake/judge",
            judge_api_key="key",
            max_retries=0,
            session=mock_session,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_run_judge_handles_invalid_json(self):
        """Invalid JSON in response → None after retries exhausted."""
        mock_session = _make_mock_session(200, {
            "choices": [{"message": {"content": "not valid json at all"}}]
        })

        result = await run_judge(
            prompt="test",
            response_a="A",
            response_b="B",
            judge_api_url="http://fake/judge",
            judge_api_key="key",
            max_retries=0,
            session=mock_session,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_run_judge_missing_credentials(self):
        """No API key → returns None immediately."""
        result = await run_judge(
            prompt="test",
            response_a="A",
            response_b="B",
            judge_api_url="",
            judge_api_key="",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_run_judge_http_error(self):
        """HTTP 500 from judge API → None."""
        mock_session = _make_mock_session(500, {})

        result = await run_judge(
            prompt="test",
            response_a="A",
            response_b="B",
            judge_api_url="http://fake/judge",
            judge_api_key="key",
            max_retries=0,
            session=mock_session,
        )

        assert result is None
