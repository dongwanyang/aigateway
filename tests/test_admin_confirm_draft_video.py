"""Coverage for admin_routes.confirm_draft video/error branches.

The branch:
- added `logger.error(..., exc_info=True)` on confirm_draft exception (regression guard)
- added VideoSubmitResult branch returning {video_id, status, media_type}
- added _record_request_log call for video confirms (failure swallowed)

Existing test_draft_routes.py covers the happy video return. These cover:
- confirm failure logs + returns 400 draft_confirm_failed
- _record_request_log failure in video path is swallowed (still returns video_id)
- video path returns 200 with media_type=video when _record_request_log succeeds
"""

import os
import sys

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.pipelines.generation._common.models import VideoSubmitResult


class _FakeDraft:
    user_id = None
    group_id = None


class _FakeRequest:
    def __init__(self):
        self.app = type("App", (), {"state": type("State", (), {})()})


@pytest.fixture
def monkeypatched_video_strategy(monkeypatch):
    """Inject a mock draft strategy returning a VideoSubmitResult."""
    from aigateway_api import admin_routes

    strategy = AsyncMock()
    strategy.get_draft = AsyncMock(return_value=_FakeDraft())
    strategy.confirm_draft = AsyncMock(return_value=VideoSubmitResult(
        draft_id="d_v", video_id="vid_xyz", status="generating"
    ))
    monkeypatch.setattr(admin_routes, "_get_draft_strategy", lambda: strategy)
    return strategy


@pytest.mark.asyncio
async def test_confirm_draft_failure_logs_and_returns_400(monkeypatch):
    """confirm_draft raising → 400 draft_confirm_failed + logger.error called with exc_info."""
    from aigateway_api import admin_routes

    strategy = AsyncMock()
    strategy.get_draft = AsyncMock(return_value=_FakeDraft())
    strategy.confirm_draft = AsyncMock(side_effect=RuntimeError("bridge down"))
    monkeypatch.setattr(admin_routes, "_get_draft_strategy", lambda: strategy)

    with patch.object(admin_routes.logger, "error") as mock_error:
        with pytest.raises(Exception) as exc_info:
            await admin_routes.confirm_draft(
                "d_fail", _FakeRequest(), {"user_id": "", "group_id": ""}
            )
        err = exc_info.value
        assert err.status_code == 400
        assert err.detail["error"]["code"] == "draft_confirm_failed"
        # logger.error called with exc_info=True
        assert mock_error.called
        # The confirm-failure log call includes exc_info=True as a kwarg
        confirm_log_calls = [
            c for c in mock_error.call_args_list
            if "draft confirm failed" in (c.args[0] if c.args else "")
        ]
        assert confirm_log_calls, "expected a 'draft confirm failed' error log"
        assert confirm_log_calls[0].kwargs.get("exc_info") is True


@pytest.mark.asyncio
async def test_confirm_video_record_log_failure_swallowed(monkeypatch, monkeypatched_video_strategy):
    """If _record_request_log raises, video confirm still returns the video_id dict."""
    from aigateway_api import admin_routes
    from aigateway_api import openai_compat

    # Force _record_request_log to raise; must patch where confirm_draft imports it from.
    with patch.object(openai_compat, "_record_request_log", new=AsyncMock(side_effect=RuntimeError("log db down"))):
        with patch.object(admin_routes.logger, "warning") as mock_warn:
            resp = await admin_routes.confirm_draft(
                "d_v", _FakeRequest(), {"user_id": "", "group_id": ""}
            )
    assert isinstance(resp, dict)
    assert resp["video_id"] == "vid_xyz"
    assert resp["media_type"] == "video"
    assert mock_warn.called  # swallowed failure was warned


@pytest.mark.asyncio
async def test_confirm_video_record_log_success_path(monkeypatch, monkeypatched_video_strategy):
    """Happy path: _record_request_log awaited once, returns video dict."""
    from aigateway_api import openai_compat

    mock_record = AsyncMock()
    with patch.object(openai_compat, "_record_request_log", new=mock_record):
        resp = await __import__(
            "aigateway_api.admin_routes", fromlist=["confirm_draft"]
        ).confirm_draft(
            "d_v", _FakeRequest(), {"user_id": "", "group_id": ""}
        )
    assert resp["video_id"] == "vid_xyz"
    mock_record.assert_awaited_once()
    # endpoint logged under the draft confirm path
    call_kwargs = mock_record.call_args.kwargs
    assert "/admin/draft/" in call_kwargs["endpoint"]
    assert call_kwargs["status_code"] == 200
    assert call_kwargs["model"] == "agnes-video-v2.0"
