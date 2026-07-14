"""draft_routes.py 单元测试.

覆盖:
- DraftActionRequest 校验 (confirm/reject/invalid)
- DraftActionResponse 模型结构
- draft_action 端点: confirm 成功路径、reject 成功路径、strategy 缺失(503)、
  各种错误码映射(404/409/410/429/500)
- _get_draft_strategy 辅助函数: app.state 两级查找
"""

import json
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from pydantic import ValidationError

from aigateway_api.draft_routes import (
    DraftActionRequest,
    DraftActionResponse,
    _get_draft_strategy,
    router,
)


# ==================================================================
# DraftActionRequest 校验测试
# ==================================================================


class TestDraftActionRequest:
    """DraftActionRequest 模型校验测试。"""

    def test_valid_confirm(self):
        req = DraftActionRequest(action="confirm")
        assert req.action == "confirm"

    def test_valid_reject(self):
        req = DraftActionRequest(action="reject")
        assert req.action == "reject"

    def test_invalid_action_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            DraftActionRequest(action="approve")
        assert "action" in str(exc_info.value)

    def test_invalid_action_values(self):
        """多种非法 action 值均应抛出 ValidationError。"""
        for bad in ("", "confirm ", "REJECT", "scale", "cancel"):
            with pytest.raises(ValidationError):
                DraftActionRequest(action=bad)


# ==================================================================
# DraftActionResponse 模型测试
# ==================================================================


class TestDraftActionResponse:
    """DraftActionResponse 模型结构测试。"""

    def test_minimal_fields(self):
        resp = DraftActionResponse(
            draft_id="d-123",
            action="confirm",
            status="confirmed",
        )
        assert resp.draft_id == "d-123"
        assert resp.action == "confirm"
        assert resp.status == "confirmed"
        assert resp.data == {}

    def test_with_data(self):
        resp = DraftActionResponse(
            draft_id="d-456",
            action="reject",
            status="regenerated",
            data={"new_draft_id": "d-789", "preview_count": 3},
        )
        assert resp.data["new_draft_id"] == "d-789"
        assert resp.data["preview_count"] == 3

    def test_data_accepts_any_dict(self):
        resp = DraftActionResponse(
            draft_id="d-000",
            action="confirm",
            status="confirmed",
            data={"target_resolution": [1920, 1080], "algorithm_used": "esrgan"},
        )
        assert resp.data["target_resolution"] == [1920, 1080]


# ==================================================================
# _get_draft_strategy 辅助函数测试
# ==================================================================


class MockState:
    """Fake FastAPI app.state for testing."""
    def __init__(self):
        self.draft_generator_strategy = None
        self.generation_optimization = None


class TestGetDraftStrategy:
    """_get_draft_strategy 查找路径测试。"""

    def test_returns_none_when_no_strategy(self):
        state = MockState()
        request = type("Req", (), {"app": type("App", (), {"state": state})()})()
        assert _get_draft_strategy(request) is None

    def test_finds_from_app_state_direct(self):
        strategy = type("MockStrategy", (), {})()
        state = MockState()
        state.draft_generator_strategy = strategy
        request = type("Req", (), {"app": type("App", (), {"state": state})()})()
        assert _get_draft_strategy(request) is strategy

    def test_finds_from_generation_optimization_namespace(self):
        strategy = type("MockStrategy", (), {})()
        state = MockState()
        gen_opt = type("GenOpt", (), {"draft_generator_strategy": strategy})()
        state.generation_optimization = gen_opt
        request = type("Req", (), {"app": type("App", (), {"state": state})()})()
        assert _get_draft_strategy(request) is strategy

    def test_direct_takes_priority_over_namespace(self):
        direct = type("DirectStrategy", (), {})()
        ns = type("NamespaceStrategy", (), {})()
        state = MockState()
        state.draft_generator_strategy = direct
        gen_opt = type("GenOpt", (), {"draft_generator_strategy": ns})()
        state.generation_optimization = gen_opt
        request = type("Req", (), {"app": type("App", (), {"state": state})()})()
        assert _get_draft_strategy(request) is direct

    def test_handles_missing_namespace_attr(self):
        state = MockState()
        state.generation_optimization = None
        request = type("Req", (), {"app": type("App", (), {"state": state})()})()
        assert _get_draft_strategy(request) is None


# ==================================================================
# draft_action 端点集成测试（带 mock strategy）
# ==================================================================


class MockConfirmResult:
    """Mock for strategy.confirm_draft return value."""
    def __init__(self):
        self.target_resolution = [1920, 1080]
        self.algorithm_used = "esrgan"
        self.duration_ms = 342.5


class MockNewDraft:
    """Mock for strategy.reject_draft return value."""
    def __init__(self):
        self.draft_id = "d-new-abc"
        self.previews = ["p1", "p2", "p3"]
        self.attempt_number = 2
        self.max_attempts = 5
        self.expires_at = "2026-07-15T00:00:00Z"


class MockStrategy:
    """Mock DraftGeneratorStrategy."""

    def __init__(self, confirm_result=None, reject_result=None, raise_exc=None):
        self._confirm_result = confirm_result
        self._reject_result = reject_result
        self._raise_exc = raise_exc
        self.confirm_called = False
        self.reject_called = False

    async def confirm_draft(self, draft_id):
        self.confirm_called = True
        if self._raise_exc:
            raise self._raise_exc
        return self._confirm_result

    async def reject_draft(self, draft_id):
        self.reject_called = True
        if self._raise_exc:
            raise self._raise_exc
        return self._reject_result


class FakeState:
    def __init__(self, strategy=None):
        self.draft_generator_strategy = strategy


class FakeApp:
    def __init__(self, strategy=None):
        self.state = FakeState(strategy)


class FakeRequest:
    def __init__(self, strategy=None):
        self.app = FakeApp(strategy)
        self.state = type("State", (), {"trace_id": "t-001"})()


class TestDraftActionEndpoint:
    """draft_action 端点行为测试。"""

    def _make_request(self, action, strategy=None):
        """Helper to create a fake ASGI-style request."""
        req = FakeRequest(strategy)
        body = DraftActionRequest(action=action)
        return req, body

    @pytest.mark.asyncio
    async def test_confirm_success(self):
        strategy = MockStrategy(confirm_result=MockConfirmResult())
        req, body = self._make_request("confirm", strategy)
        from aigateway_api.draft_routes import draft_action
        resp = await draft_action("d-123", body, req)
        assert isinstance(resp, DraftActionResponse)
        assert resp.draft_id == "d-123"
        assert resp.action == "confirm"
        assert resp.status == "confirmed"
        assert resp.data["target_resolution"] == [1920, 1080]
        assert resp.data["algorithm_used"] == "esrgan"
        assert strategy.confirm_called

    @pytest.mark.asyncio
    async def test_reject_success(self):
        strategy = MockStrategy(reject_result=MockNewDraft())
        req, body = self._make_request("reject", strategy)
        from aigateway_api.draft_routes import draft_action
        resp = await draft_action("d-456", body, req)
        assert isinstance(resp, DraftActionResponse)
        assert resp.action == "reject"
        assert resp.status == "regenerated"
        assert resp.draft_id == "d-new-abc"
        assert resp.data["preview_count"] == 3
        assert resp.data["attempt_number"] == 2
        assert strategy.reject_called

    @pytest.mark.asyncio
    async def test_strategy_not_available_returns_503(self):
        """strategy 为 None 时应返回 503。"""
        req, body = self._make_request("confirm", strategy=None)
        from aigateway_api.draft_routes import draft_action
        with pytest.raises(Exception) as exc_info:
            await draft_action("d-xxx", body, req)
        err = exc_info.value
        assert err.status_code == 503
        assert "service_unavailable" in err.detail["error"]["code"]

    @pytest.mark.asyncio
    async def test_confirm_draft_not_found_returns_404(self):
        strategy = MockStrategy(
            confirm_result=None,
            raise_exc=type("DraftWorkflowError", (Exception,), {})("Draft not found or expired: d-123")
        )
        req, body = self._make_request("confirm", strategy)
        from aigateway_api.draft_routes import draft_action
        with pytest.raises(Exception) as exc_info:
            await draft_action("d-123", body, req)
        err = exc_info.value
        assert err.status_code == 404
        assert err.detail["error"]["code"] == "draft_not_found"

    @pytest.mark.asyncio
    async def test_confirm_state_conflict_returns_409(self):
        strategy = MockStrategy(
            raise_exc=type("DraftWorkflowError", (Exception,), {})("Draft cannot be confirmed: already confirmed")
        )
        req, body = self._make_request("confirm", strategy)
        from aigateway_api.draft_routes import draft_action
        with pytest.raises(Exception) as exc_info:
            await draft_action("d-already", body, req)
        err = exc_info.value
        assert err.status_code == 409
        assert err.detail["error"]["code"] == "draft_state_conflict"

    @pytest.mark.asyncio
    async def test_reject_state_conflict_returns_409(self):
        strategy = MockStrategy(
            raise_exc=type("DraftWorkflowError", (Exception,), {})("Draft cannot be rejected")
        )
        req, body = self._make_request("reject", strategy)
        from aigateway_api.draft_routes import draft_action
        with pytest.raises(Exception) as exc_info:
            await draft_action("d-rej", body, req)
        err = exc_info.value
        assert err.status_code == 409

    @pytest.mark.asyncio
    async def test_draft_expired_returns_410(self):
        strategy = MockStrategy(
            raise_exc=type("DraftWorkflowError", (Exception,), {})("Draft has expired: d-old")
        )
        req, body = self._make_request("confirm", strategy)
        from aigateway_api.draft_routes import draft_action
        with pytest.raises(Exception) as exc_info:
            await draft_action("d-old", body, req)
        err = exc_info.value
        assert err.status_code == 410
        assert err.detail["error"]["code"] == "draft_expired"

    @pytest.mark.asyncio
    async def test_regeneration_limit_returns_429(self):
        strategy = MockStrategy(
            raise_exc=type("DraftWorkflowError", (Exception,), {})("Regeneration limit reached")
        )
        req, body = self._make_request("reject", strategy)
        from aigateway_api.draft_routes import draft_action
        with pytest.raises(Exception) as exc_info:
            await draft_action("d-limited", body, req)
        err = exc_info.value
        assert err.status_code == 429
        assert err.detail["error"]["code"] == "regeneration_limit_reached"

    @pytest.mark.asyncio
    async def test_unknown_error_returns_500(self):
        strategy = MockStrategy(
            raise_exc=Exception("Unexpected database connection lost")
        )
        req, body = self._make_request("confirm", strategy)
        from aigateway_api.draft_routes import draft_action
        with pytest.raises(Exception) as exc_info:
            await draft_action("d-err", body, req)
        err = exc_info.value
        assert err.status_code == 500
        assert err.detail["error"]["code"] == "internal_error"

    @pytest.mark.asyncio
    async def test_confirm_response_structure(self):
        """confirm 响应应包含完整的结构化数据。"""
        strategy = MockStrategy(confirm_result=MockConfirmResult())
        req, body = self._make_request("confirm", strategy)
        from aigateway_api.draft_routes import draft_action
        resp = await draft_action("d-resp", body, req)
        assert resp.draft_id == "d-resp"
        assert resp.action == "confirm"
        assert resp.status == "confirmed"
        assert "target_resolution" in resp.data
        assert "algorithm_used" in resp.data
        assert "duration_ms" in resp.data

    @pytest.mark.asyncio
    async def test_reject_response_structure(self):
        """reject 响应应包含新草图信息。"""
        strategy = MockStrategy(reject_result=MockNewDraft())
        req, body = self._make_request("reject", strategy)
        from aigateway_api.draft_routes import draft_action
        resp = await draft_action("d-rej-resp", body, req)
        assert resp.draft_id == "d-new-abc"
        assert resp.action == "reject"
        assert resp.status == "regenerated"
        assert "new_draft_id" in resp.data
        assert "preview_count" in resp.data
        assert "attempt_number" in resp.data
        assert "max_attempts" in resp.data
        assert "expires_at" in resp.data


# ==================================================================
# Router 注册测试
# ==================================================================


class TestRouterRegistration:
    """验证 router 是否正确注册了端点。"""

    def test_router_has_prefix(self):
        assert router.prefix == "/api/v1/generation"

    def test_router_has_tags(self):
        assert "generation-optimization" in router.tags

    def test_route_exists_for_draft_action(self):
        paths = {}
        for route in router.routes:
            paths.setdefault(route.path, {})
            for method in route.methods:
                paths[route.path][method] = route.name
        # Router has prefix "/api/v1/generation"
        full_path = "/api/v1/generation/drafts/{draft_id}/action"
        assert full_path in paths
        assert "POST" in paths[full_path]
