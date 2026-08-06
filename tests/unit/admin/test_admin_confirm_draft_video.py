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
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.pipelines.generation._common.models import (
    UpscaleResult,
    VideoSubmitResult,
)

_AUTH = {"user_id": "test-user", "group_id": ""}


class _FakeDraft:
    user_id = "test-user"
    group_id = None
    media_type = "image"
    workflow_version = ""
    generation_params = {"routed_model": "agnes-video-v2.1"}


class _FakeLocalVideoDraft(_FakeDraft):
    media_type = "video"


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
                "d_fail", _FakeRequest(), _AUTH
            )
        err = exc_info.value
        assert err.status_code == 400
        assert err.detail["error"]["code"] == "draft_confirm_failed"
        assert mock_error.called
        confirm_log_calls = [
            c for c in mock_error.call_args_list
            if "draft confirm failed" in (c.args[0] if c.args else "")
        ]
        assert confirm_log_calls, "expected a 'draft confirm failed' error log"
        assert confirm_log_calls[0].kwargs.get("exc_info") is True


@pytest.mark.asyncio
async def test_confirm_video_record_log_failure_swallowed(monkeypatch, monkeypatched_video_strategy):
    """If _record_request_log raises, video confirm still returns the video_id dict."""
    from aigateway_api import admin_routes, openai_compat

    with patch.object(openai_compat, "_record_request_log", new=AsyncMock(side_effect=RuntimeError("log db down"))):
        with patch.object(admin_routes.logger, "warning") as mock_warn:
            resp = await admin_routes.confirm_draft(
                "d_v", _FakeRequest(), _AUTH
            )
    assert isinstance(resp, dict)
    assert resp["video_id"] == "vid_xyz"
    assert resp["media_type"] == "video"
    assert mock_warn.called


@pytest.mark.asyncio
async def test_confirm_video_record_log_success_path(monkeypatch, monkeypatched_video_strategy):
    """Happy path: _record_request_log awaited once, returns video dict."""
    from aigateway_api import openai_compat

    mock_record = AsyncMock()
    with patch.object(openai_compat, "_record_request_log", new=mock_record):
        resp = await __import__(
            "aigateway_api.admin_routes", fromlist=["confirm_draft"]
        ).confirm_draft(
            "d_v", _FakeRequest(), _AUTH
        )
    assert resp["video_id"] == "vid_xyz"
    mock_record.assert_awaited_once()
    call_kwargs = mock_record.call_args.kwargs
    assert "/admin/draft/" in call_kwargs["endpoint"]
    assert call_kwargs["status_code"] == 200
    # 模型名必须来自本次实际路由，而不是写死的字符串:否则日志无法用于核对
    # 真实使用的视频模型与成本。
    assert call_kwargs["model"] == "agnes-video-v2.1"
    # 耗时同样必须是真实测量值，之前恒为 0.0。
    assert call_kwargs["duration_ms"] >= 0.0


@pytest.mark.asyncio
async def test_confirm_video_log_model_falls_back_to_workflow_version(monkeypatch):
    """无 routed_model 时回退到工作流版本，仍不写死模型名。"""
    from aigateway_api import admin_routes, openai_compat

    class _NoRoutedModelDraft(_FakeDraft):
        media_type = "video"
        workflow_version = "wan2.2-ti2v-5b-v1"
        generation_params: dict = {}

    strategy = AsyncMock()
    strategy.get_draft = AsyncMock(return_value=_NoRoutedModelDraft())
    strategy.confirm_draft = AsyncMock(return_value=VideoSubmitResult(
        draft_id="d_v", video_id="vid_fallback", status="generating"
    ))
    monkeypatch.setattr(admin_routes, "_get_draft_strategy", lambda: strategy)

    mock_record = AsyncMock()
    with patch.object(openai_compat, "_record_request_log", new=mock_record):
        resp = await admin_routes.confirm_draft("d_v", _FakeRequest(), _AUTH)

    assert resp["video_id"] == "vid_fallback"
    assert mock_record.call_args.kwargs["model"] == "comfyui:video:wan2.2-ti2v-5b-v1"


@pytest.mark.asyncio
async def test_confirm_local_comfyui_video_returns_mp4_data_url(monkeypatch):
    """The normal video path returns persisted ComfyUI MP4 bytes, not a video id."""
    from aigateway_api import admin_routes

    strategy = AsyncMock()
    strategy.get_draft = AsyncMock(return_value=_FakeLocalVideoDraft())
    strategy.confirm_draft = AsyncMock(
        return_value=UpscaleResult(
            draft_id="d_local",
            output_data=b"\x00\x00\x00\x18ftypmp42video",
            target_resolution=(512, 288),
            algorithm_used="comfyui:wan2.2-ti2v-5b-v1",
            duration_ms=1000,
        )
    )
    monkeypatch.setattr(admin_routes, "_get_draft_strategy", lambda: strategy)

    response = await admin_routes.confirm_draft(
        "d_local", _FakeRequest(), _AUTH
    )

    assert response["media_type"] == "video"
    assert response["upscaled_url"].startswith("data:video/mp4;base64,")
    assert "video_id" not in response
    assert response["algorithm"] == "comfyui:wan2.2-ti2v-5b-v1"


@pytest.mark.asyncio
async def test_confirm_video_upstream_503_returns_502(monkeypatch):
    """上游 Agnes /videos 返回 503(瞬时不可用) → 502 upstream_unavailable + retryable。"""
    from aigateway_api import admin_routes
    from aigateway_core.pipelines.generation._common.exceptions import (
        DraftWorkflowError,
    )

    err = DraftWorkflowError(
        "upstream_unavailable: Agnes /videos submission failed: 503"
    )
    err.upstream_status = 503

    strategy = AsyncMock()
    strategy.get_draft = AsyncMock(return_value=_FakeDraft())
    strategy.confirm_draft = AsyncMock(side_effect=err)
    monkeypatch.setattr(admin_routes, "_get_draft_strategy", lambda: strategy)

    with patch.object(admin_routes.logger, "warning") as mock_warn:
        with patch.object(admin_routes.logger, "error") as mock_error:
            with pytest.raises(Exception) as exc_info:
                await admin_routes.confirm_draft(
                    "d_up", _FakeRequest(), _AUTH
                )
    raised = exc_info.value
    assert raised.status_code == 502
    assert raised.detail["error"]["code"] == "upstream_unavailable"
    assert raised.detail["error"]["retryable"] is True
    assert raised.detail["error"]["upstream_status"] == 503
    assert mock_warn.called
    mock_error.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_video_network_error_returns_502(monkeypatch):
    """上游 Agnes 网络故障 → 502 upstream_unavailable + retryable。"""
    from aigateway_api import admin_routes
    from aigateway_core.pipelines.generation._common.exceptions import (
        DraftWorkflowError,
    )

    err = DraftWorkflowError(
        "upstream_unavailable: Agnes /videos submission failed (network error): connect refused"
    )
    err.upstream_unavailable = True

    strategy = AsyncMock()
    strategy.get_draft = AsyncMock(return_value=_FakeDraft())
    strategy.confirm_draft = AsyncMock(side_effect=err)
    monkeypatch.setattr(admin_routes, "_get_draft_strategy", lambda: strategy)

    with patch.object(admin_routes.logger, "warning") as mock_warn:
        with patch.object(admin_routes.logger, "error") as mock_error:
            with pytest.raises(Exception) as exc_info:
                await admin_routes.confirm_draft(
                    "d_net", _FakeRequest(), _AUTH
                )
    raised = exc_info.value
    assert raised.status_code == 502
    assert raised.detail["error"]["code"] == "upstream_unavailable"
    assert raised.detail["error"]["retryable"] is True
    assert "upstream_status" not in raised.detail["error"]
    assert mock_warn.called
    mock_error.assert_not_called()
