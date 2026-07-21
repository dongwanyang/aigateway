"""Video Routes 单元测试

覆盖 GET /v1/videos/{id} endpoint:
- bridge 未初始化时返回 503
- bridge 正常返回视频状态
- bridge 异常时返回 502 且不暴露内部错误细节
- debug 维度开启时暴露详细错误信息

测试策略：直接测试 handler 函数而非完整 HTTP 栈，避免 auth_middleware 依赖。
"""

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway_api", "src"))


# ==================================================================
# Helper
# ==================================================================


def _make_mock_state(bridge=None):
    """Create a mock state object."""
    state = MagicMock()
    state.litellm_bridge = bridge
    return state


async def _mock_request():
    """Create a minimal mock request object."""
    request = MagicMock()
    request.state.trace_id = "test-trace"
    return request


# ==================================================================
# Direct Handler Tests (bypassing auth middleware)
# ==================================================================


class TestRetrieveVideoHandler:
    """测试视频轮询 handler 逻辑（绕过 auth）。"""

    @pytest.mark.asyncio
    async def test_bridge_unavailable(self):
        """bridge 未初始化时返回 503。"""
        from aigateway_api.video_routes import retrieve_video

        state = _make_mock_state(bridge=None)
        request = await _mock_request()

        with patch("aigateway_api.video_routes.get_state", return_value=state):
            response = await retrieve_video(video_id="test-video-id", request=request, _auth={})

        assert response.status_code == 503
        assert response.body == b'{"error":{"code":"bridge_unavailable","message":"LiteLLM bridge not initialized"}}'

    @pytest.mark.asyncio
    async def test_success(self):
        """bridge 正常返回视频状态。"""
        from aigateway_api.video_routes import retrieve_video

        bridge = MagicMock()
        bridge.retrieve_video = AsyncMock(return_value={
            "id": "vid-123",
            "status": "succeeded",
            "video_url": "https://cdn.example.com/video.mp4",
        })

        state = _make_mock_state(bridge=bridge)
        request = await _mock_request()

        with patch("aigateway_api.video_routes.get_state", return_value=state):
            response = await retrieve_video(video_id="vid-123", request=request, _auth={})

        assert response.status_code == 200
        data = json.loads(response.body)
        assert data["id"] == "vid-123"
        assert data["status"] == "succeeded"
        bridge.retrieve_video.assert_called_once_with("vid-123")

    @pytest.mark.asyncio
    async def test_error_generic_message(self):
        """bridge 异常时返回通用错误消息（不暴露内部细节）。"""
        from aigateway_api.video_routes import retrieve_video

        bridge = MagicMock()
        bridge.retrieve_video = AsyncMock(
            side_effect=Exception("Provider API timeout with sensitive URL https://user:pass@internal.example.com")
        )

        state = _make_mock_state(bridge=bridge)
        request = await _mock_request()

        with patch("aigateway_api.video_routes.get_state", return_value=state):
            response = await retrieve_video(video_id="vid-error", request=request, _auth={})

        assert response.status_code == 502
        data = json.loads(response.body)
        assert data["error"]["code"] == "video_retrieve_failed"
        # 默认不暴露详细错误
        assert "sensitive" not in data["error"]["message"]
        assert "internal.example.com" not in data["error"]["message"]

    @patch("aigateway_core.shared.trace_event.TraceCollector")
    @pytest.mark.asyncio
    async def test_error_debug_detail(self, mock_trace_collector_cls):
        """debug 维度开启时暴露详细错误信息。"""
        from aigateway_api.video_routes import retrieve_video

        # Mock TraceCollector
        mock_collector = MagicMock()
        mock_collector.current.return_value = mock_collector
        mock_collector.get_debug_dimension.return_value = True
        mock_trace_collector_cls.current = MagicMock(return_value=mock_collector)

        bridge = MagicMock()
        bridge.retrieve_video = AsyncMock(
            side_effect=Exception("Provider API timeout https://user:pass@internal.example.com")
        )

        state = _make_mock_state(bridge=bridge)
        request = await _mock_request()

        with patch("aigateway_api.video_routes.get_state", return_value=state):
            response = await retrieve_video(video_id="vid-debug", request=request, _auth={})

        assert response.status_code == 502
        data = json.loads(response.body)
        # debug 模式下暴露详细错误
        assert "internal.example.com" in data["error"]["message"]
