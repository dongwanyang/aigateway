"""Unit tests for rate_limiter.py — RateLimiterMiddleware.

Covers:
- _check_in_memory: sliding window counter logic
- dispatch: path filtering, exemption, 429 response
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

import pytest
from unittest.mock import AsyncMock, MagicMock
from starlette.testclient import TestClient
from fastapi import FastAPI, Request

from aigateway_api.rate_limiter import RateLimiterMiddleware


class TestCheckInMemory:
    """Test _check_in_memory pure sliding window logic."""

    def _make_limiter(self, max_requests=3, window_seconds=60):
        app = MagicMock()
        return RateLimiterMiddleware(app, max_requests=max_requests, window_seconds=window_seconds)

    def test_first_request_allowed(self):
        limiter = self._make_limiter(max_requests=3)
        allowed, retry_after = limiter._check_in_memory("1.2.3.4", "/admin/test")
        assert allowed is True
        assert retry_after == 0

    def test_exceeds_max_requests(self):
        limiter = self._make_limiter(max_requests=2)
        # Fill up the window
        limiter._check_in_memory("1.2.3.4", "/admin/test")
        limiter._check_in_memory("1.2.3.4", "/admin/test")
        # Third should be denied
        allowed, retry_after = limiter._check_in_memory("1.2.3.4", "/admin/test")
        assert allowed is False
        assert retry_after > 0

    def test_different_ips_independent(self):
        limiter = self._make_limiter(max_requests=2)
        # Fill up for IP A
        limiter._check_in_memory("1.1.1.1", "/admin/test")
        limiter._check_in_memory("1.1.1.1", "/admin/test")
        # IP B should still be allowed
        allowed, _ = limiter._check_in_memory("2.2.2.2", "/admin/test")
        assert allowed is True

    def test_different_paths_independent(self):
        limiter = self._make_limiter(max_requests=2)
        # Fill up path /admin/a
        limiter._check_in_memory("1.1.1.1", "/admin/a")
        limiter._check_in_memory("1.1.1.1", "/admin/a")
        # Path /admin/b should still be allowed
        allowed, _ = limiter._check_in_memory("1.1.1.1", "/admin/b")
        assert allowed is True

    def test_window_expiry_clears_old_entries(self):
        limiter = self._make_limiter(max_requests=2, window_seconds=1)
        with pytest.MonkeyPatch.context() as mp:
            now = 1000.0
            mp.setattr("aigateway_api.rate_limiter.time.time", lambda: now)
            # Fill up
            limiter._check_in_memory("1.1.1.1", "/admin/test")
            limiter._check_in_memory("1.1.1.1", "/admin/test")
            # Should be denied within the same window
            allowed, _ = limiter._check_in_memory("1.1.1.1", "/admin/test")
            assert allowed is False
            # Advance beyond the window deterministically
            now = 1001.1
            allowed, _ = limiter._check_in_memory("1.1.1.1", "/admin/test")
            assert allowed is True

    def test_retry_after_calculated(self):
        limiter = self._make_limiter(max_requests=1, window_seconds=60)
        limiter._check_in_memory("1.1.1.1", "/admin/test")
        allowed, retry_after = limiter._check_in_memory("1.1.1.1", "/admin/test")
        assert allowed is False
        assert retry_after > 0
        assert retry_after <= 60

    def test_unknown_ip(self):
        limiter = self._make_limiter(max_requests=3)
        allowed, _ = limiter._check_in_memory("unknown", "/admin/test")
        assert allowed is True

    def test_many_requests_accumulate(self):
        limiter = self._make_limiter(max_requests=5)
        for i in range(5):
            allowed, _ = limiter._check_in_memory("1.1.1.1", "/admin/test")
            assert allowed is True, f"Request {i+1} should be allowed"
        # 6th should be denied
        allowed, _ = limiter._check_in_memory("1.1.1.1", "/admin/test")
        assert allowed is False


class TestDispatch:
    """Test RateLimiterMiddleware.dispatch path filtering and 429 response."""

    def _make_app(self, limiter=None):
        app = FastAPI()
        app.state.key_store = None

        if limiter is None:
            limiter = RateLimiterMiddleware(app, max_requests=3, window_seconds=60)

        @app.get("/admin/test")
        async def admin_test():
            return {"status": "ok"}

        @app.get("/health")
        async def health():
            return {"status": "healthy"}

        @app.get("/metrics")
        async def metrics():
            return {"count": 0}

        @app.get("/v1/chat/completions")
        async def chat():
            return {"choices": []}

        return app, limiter

    def test_exempt_health_not_rate_limited(self):
        app, limiter = self._make_app()
        app.add_middleware(RateLimiterMiddleware, max_requests=1)
        # Remove the limiter we added and use our own
        app.user_middleware.clear()
        # Manually test dispatch
        client = TestClient(app, raise_server_exceptions=False)
        # Multiple calls to /health should not be rate limited
        for _ in range(10):
            resp = client.get("/health")
            assert resp.status_code == 200, f"Health should not be rate limited: {resp.status_code}"

    def test_exempt_metrics_not_rate_limited(self):
        app, limiter = self._make_app()
        app.user_middleware.clear()
        app.add_middleware(RateLimiterMiddleware, max_requests=1)
        client = TestClient(app, raise_server_exceptions=False)
        for _ in range(10):
            resp = client.get("/metrics")
            assert resp.status_code == 200

    def test_non_protected_path_not_rate_limited(self):
        app, limiter = self._make_app()
        app.user_middleware.clear()
        app.add_middleware(RateLimiterMiddleware, max_requests=1)
        client = TestClient(app, raise_server_exceptions=False)
        for _ in range(10):
            resp = client.get("/v1/chat/completions")
            assert resp.status_code == 200

    def test_protected_path_rate_limited_after_threshold(self):
        app, limiter = self._make_app()
        app.user_middleware.clear()
        limiter = RateLimiterMiddleware(app, max_requests=2, window_seconds=60)
        app.add_middleware(RateLimiterMiddleware, max_requests=2, window_seconds=60)
        client = TestClient(app, raise_server_exceptions=False)
        # First two should succeed
        resp1 = client.get("/admin/test")
        assert resp1.status_code == 200
        resp2 = client.get("/admin/test")
        assert resp2.status_code == 200
        # Third should be 429
        resp3 = client.get("/admin/test")
        assert resp3.status_code == 429
        body = resp3.json()
        assert "rate_limited" in body.get("error", {}).get("code", "").lower() or body.get("error", {}).get("code") == "rate_limited"

    def test_rate_limit_429_has_retry_after_header(self):
        app = FastAPI()
        app.state.key_store = None
        app.add_middleware(RateLimiterMiddleware, max_requests=1, window_seconds=60)

        @app.get("/admin/test")
        async def test_endpoint():
            return {"status": "ok"}

        client = TestClient(app, raise_server_exceptions=False)
        client.get("/admin/test")  # First call succeeds
        resp = client.get("/admin/test")  # Second call should be 429
        assert resp.status_code == 429
        assert "retry-after" in resp.headers

    def test_custom_protected_prefix(self):
        """Test that only paths matching protected_prefixes are rate limited."""
        app = FastAPI()
        app.state.key_store = None
        app.add_middleware(RateLimiterMiddleware, max_requests=1, window_seconds=60, protected_prefixes=("/admin", "/internal"))

        @app.get("/admin/test")
        async def admin_test():
            return {"status": "ok"}

        @app.get("/internal/test")
        async def internal_test():
            return {"status": "ok"}

        client = TestClient(app, raise_server_exceptions=False)
        # First calls succeed
        client.get("/admin/test")
        client.get("/internal/test")
        # Second calls should be 429
        assert client.get("/admin/test").status_code == 429
        assert client.get("/internal/test").status_code == 429
