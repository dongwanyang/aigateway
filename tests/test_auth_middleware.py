"""Unit tests for auth_middleware functions.

Covers:
- _extract_api_key: Bearer and x-api-key header parsing
- _hash_key: SHA-256 hashing
- authenticate / authenticate_admin / require_api_key: mocked key_store flows
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from starlette.testclient import TestClient
from fastapi import FastAPI, Request, Depends


class TestExtractApiKey:
    """Test _extract_api_key pure function."""

    def test_bearer_header(self):
        from aigateway_api.auth_middleware import _extract_api_key
        result = _extract_api_key(authorization="Bearer my-secret-key")
        assert result == "my-secret-key"

    def test_bearer_case_insensitive(self):
        from aigateway_api.auth_middleware import _extract_api_key
        result = _extract_api_key(authorization="BEARER my-secret-key")
        assert result == "my-secret-key"

    def test_bearer_with_spaces(self):
        from aigateway_api.auth_middleware import _extract_api_key
        result = _extract_api_key(authorization="Bearer  key-with-spaces ")
        assert result == " key-with-spaces "

    def test_x_api_key_header(self):
        from aigateway_api.auth_middleware import _extract_api_key
        result = _extract_api_key(x_api_key="x-api-key-value")
        assert result == "x-api-key-value"

    def test_x_api_key_takes_precedence(self):
        from aigateway_api.auth_middleware import _extract_api_key
        result = _extract_api_key(authorization="Bearer bearer-key", x_api_key="x-api-key-value")
        assert result == "x-api-key-value"

    def test_neither_header(self):
        from aigateway_api.auth_middleware import _extract_api_key
        result = _extract_api_key()
        assert result is None

    def test_bearer_missing_scheme(self):
        from aigateway_api.auth_middleware import _extract_api_key
        result = _extract_api_key(authorization="Basic some-token")
        assert result is None

    def test_bearer_no_space(self):
        from aigateway_api.auth_middleware import _extract_api_key
        result = _extract_api_key(authorization="Bearertoken")
        assert result is None

    def test_empty_authorization(self):
        from aigateway_api.auth_middleware import _extract_api_key
        result = _extract_api_key(authorization="")
        assert result is None

    def test_empty_x_api_key(self):
        from aigateway_api.auth_middleware import _extract_api_key
        result = _extract_api_key(x_api_key="")
        assert result is None


class TestHashKey:
    """Test _hash_key pure function."""

    def test_known_key(self):
        from aigateway_api.auth_middleware import _hash_key
        # Verify deterministic output
        result = _hash_key("test-key-123")
        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)

    def test_same_input_same_output(self):
        from aigateway_api.auth_middleware import _hash_key
        r1 = _hash_key("same-key")
        r2 = _hash_key("same-key")
        assert r1 == r2

    def test_different_input_different_output(self):
        from aigateway_api.auth_middleware import _hash_key
        r1 = _hash_key("key-a")
        r2 = _hash_key("key-b")
        assert r1 != r2

    def test_empty_key(self):
        from aigateway_api.auth_middleware import _hash_key
        result = _hash_key("")
        assert len(result) == 16

    def test_unicode_key(self):
        from aigateway_api.auth_middleware import _hash_key
        result = _hash_key("key-with-unicode-é-ñ-中文")
        assert len(result) == 16


class TestAuthenticateMiddleware:
    """Test authenticate / authenticate_admin / require_api_key with mocked key_store."""

    def _make_app_with_key_store(self, key_store_mock):
        """Create a minimal FastAPI app with the given key_store mock."""
        app = FastAPI()
        app.state.key_store = key_store_mock

        @app.get("/test")
        async def test_endpoint(request: Request):
            return {"api_key_data": dict(request.state.api_key_data)}

        return app

    @pytest.mark.asyncio
    async def test_authenticate_valid_key(self):
        from aigateway_api.auth_middleware import authenticate
        from fastapi import Depends

        key_store = AsyncMock()
        key_store.validate = AsyncMock(return_value={"key_id": "key_123", "user_id": "test-user"})

        app = self._make_app_with_key_store(key_store)
        app.dependency_overrides[authenticate] = lambda: key_store.validate.return_value
        # Actually test the function directly
        result = await authenticate(MagicMock(app=MagicMock(state=MagicMock(key_store=key_store))), api_key=None, authorization="Bearer test-key")
        assert result == {"key_id": "key_123", "user_id": "test-user"}
        key_store.validate.assert_called_once_with("test-key")

    @pytest.mark.asyncio
    async def test_authenticate_missing_key_raises_401(self):
        from aigateway_api.auth_middleware import authenticate
        from fastapi import HTTPException

        key_store = AsyncMock()
        request_mock = MagicMock(app=MagicMock(state=MagicMock(key_store=key_store)))

        with pytest.raises(HTTPException) as exc_info:
            await authenticate(request_mock, api_key=None, authorization=None)
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error"]["code"] == "unauthorized"
        assert "missing api key" in exc_info.value.detail["error"]["message"].lower()
        assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}

    @pytest.mark.asyncio
    async def test_authenticate_invalid_key_raises_401(self):
        from aigateway_api.auth_middleware import authenticate
        from fastapi import HTTPException

        key_store = AsyncMock()
        key_store.validate = AsyncMock(return_value=None)
        request_mock = MagicMock(app=MagicMock(state=MagicMock(key_store=key_store)))

        with pytest.raises(HTTPException) as exc_info:
            await authenticate(request_mock, api_key=None, authorization="Bearer invalid")
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error"]["code"] == "unauthorized"
        assert "invalid or missing api key" in exc_info.value.detail["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_authenticate_revoked_key_raises_403(self):
        from aigateway_api.auth_middleware import authenticate
        from fastapi import HTTPException

        key_store = AsyncMock()
        key_store.validate = AsyncMock(side_effect=Exception("Key has been REVOKED"))
        request_mock = MagicMock(app=MagicMock(state=MagicMock(key_store=key_store)))

        with pytest.raises(HTTPException) as exc_info:
            await authenticate(request_mock, api_key=None, authorization="Bearer revoked-key")
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["error"]["code"] == "forbidden"
        assert "revoked" in exc_info.value.detail["error"]["message"].lower()
        assert "revoked-" in exc_info.value.detail["error"]["message"]

    @pytest.mark.asyncio
    async def test_authenticate_suspended_key_raises_403(self):
        from aigateway_api.auth_middleware import authenticate
        from fastapi import HTTPException

        key_store = AsyncMock()
        key_store.validate = AsyncMock(side_effect=Exception("Key is SUSPENDED"))
        request_mock = MagicMock(app=MagicMock(state=MagicMock(key_store=key_store)))

        with pytest.raises(HTTPException) as exc_info:
            await authenticate(request_mock, api_key=None, authorization="Bearer suspended-key")
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["error"]["code"] == "forbidden"
        assert "suspended" in exc_info.value.detail["error"]["message"].lower()
        assert "suspende" in exc_info.value.detail["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_authenticate_uninitialized_key_store_raises_500(self):
        from aigateway_api.auth_middleware import authenticate
        from fastapi import HTTPException

        request_mock = MagicMock(app=MagicMock(state=MagicMock(key_store=None)))

        with pytest.raises(HTTPException) as exc_info:
            await authenticate(request_mock, api_key=None, authorization="Bearer test-key")
        assert exc_info.value.status_code == 500
        assert exc_info.value.detail["error"]["code"] == "internal_error"
        assert "authentication service unavailable" in exc_info.value.detail["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_authenticate_x_api_key_header(self):
        from aigateway_api.auth_middleware import authenticate

        key_store = AsyncMock()
        key_store.validate = AsyncMock(return_value={"key_id": "key_456", "user_id": "x-user"})
        request_mock = MagicMock(app=MagicMock(state=MagicMock(key_store=key_store)))

        result = await authenticate(request_mock, api_key="x-api-key-value", authorization=None)
        assert result == {"key_id": "key_456", "user_id": "x-user"}
        key_store.validate.assert_called_once_with("x-api-key-value")

    @pytest.mark.asyncio
    async def test_authenticate_mounts_state(self):
        from aigateway_api.auth_middleware import authenticate

        key_store = AsyncMock()
        key_store.validate = AsyncMock(return_value={"key_id": "key_789", "user_id": "state-test"})
        request_mock = MagicMock(app=MagicMock(state=MagicMock(key_store=key_store)))

        await authenticate(request_mock, api_key=None, authorization="Bearer test-key")
        assert request_mock.state.api_key_data == {"key_id": "key_789", "user_id": "state-test"}
        assert request_mock.state.api_key_value == "test-key"


class TestAuthenticateAdmin:
    """Test authenticate_admin with mocked key_store."""

    @pytest.mark.asyncio
    async def test_admin_valid_key(self):
        from aigateway_api.auth_middleware import authenticate_admin

        key_store = AsyncMock()
        key_store.validate = AsyncMock(return_value={"key_id": "admin-key", "user_id": "admin", "is_admin": True})
        request_mock = MagicMock()
        request_mock.headers = {"authorization": "Bearer admin-key", "x-api-key": ""}
        request_mock.app.state.key_store = key_store

        result = await authenticate_admin(request_mock)
        assert result["key_id"] == "admin-key"
        assert result["user_id"] == "admin"
        assert result["is_admin"] is True

    @pytest.mark.asyncio
    async def test_admin_missing_key_raises_401(self):
        from aigateway_api.auth_middleware import authenticate_admin
        from fastapi import HTTPException

        key_store = AsyncMock()
        request_mock = MagicMock()
        request_mock.headers = {"authorization": "", "x-api-key": ""}
        request_mock.app.state.key_store = key_store

        with pytest.raises(HTTPException) as exc_info:
            await authenticate_admin(request_mock)
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error"]["code"] == "unauthorized"
        assert "invalid or missing api key" in exc_info.value.detail["error"]["message"].lower()
        assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}

    @pytest.mark.asyncio
    async def test_admin_revoked_key_raises_403(self):
        from aigateway_api.auth_middleware import authenticate_admin
        from fastapi import HTTPException

        key_store = AsyncMock()
        key_store.validate = AsyncMock(side_effect=Exception("Key has been revoked"))
        request_mock = MagicMock()
        request_mock.headers = {"authorization": "Bearer revoked", "x-api-key": ""}
        request_mock.app.state.key_store = key_store

        with pytest.raises(HTTPException) as exc_info:
            await authenticate_admin(request_mock)
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["error"]["code"] == "forbidden"
        assert "revoked" in exc_info.value.detail["error"]["message"].lower()


class TestRequireApiKey:
    """Test require_api_key simplified middleware."""

    @pytest.mark.asyncio
    async def test_require_api_key_valid(self):
        from aigateway_api.auth_middleware import require_api_key

        key_store = AsyncMock()
        key_store.validate = AsyncMock(return_value={"key_id": "req-key", "user_id": "req-user"})
        request_mock = MagicMock()
        request_mock.headers = {"authorization": "Bearer req-key", "x-api-key": ""}
        request_mock.app.state.key_store = key_store

        await require_api_key(request_mock)
        assert request_mock.state.api_key_data == {"key_id": "req-key", "user_id": "req-user"}
        assert request_mock.state.api_key_value == "req-key"

    @pytest.mark.asyncio
    async def test_require_api_key_missing_raises_401(self):
        from aigateway_api.auth_middleware import require_api_key
        from fastapi import HTTPException

        key_store = AsyncMock()
        request_mock = MagicMock()
        request_mock.headers = {"authorization": "", "x-api-key": ""}
        request_mock.app.state.key_store = key_store

        with pytest.raises(HTTPException) as exc_info:
            await require_api_key(request_mock)
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error"]["code"] == "unauthorized"
        assert "invalid or missing api key" in exc_info.value.detail["error"]["message"].lower()
        assert exc_info.value.headers is None

    @pytest.mark.asyncio
    async def test_require_api_key_revoked_raises_403(self):
        from aigateway_api.auth_middleware import require_api_key
        from fastapi import HTTPException

        key_store = AsyncMock()
        key_store.validate = AsyncMock(side_effect=Exception("revoked"))
        request_mock = MagicMock()
        request_mock.headers = {"authorization": "Bearer revoked-key", "x-api-key": ""}
        request_mock.app.state.key_store = key_store

        with pytest.raises(HTTPException) as exc_info:
            await require_api_key(request_mock)
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["error"]["code"] == "forbidden"
        assert "revoked" in exc_info.value.detail["error"]["message"].lower()
