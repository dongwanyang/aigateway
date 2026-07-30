"""Unit tests for QdrantClientManager — all methods mockable with httpx.AsyncClient mocks."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestQdrantClientManagerSingleton:
    """get_qdrant_manager() lazy init + idempotent."""

    def test_returns_same_instance(self):
        from aigateway_core.shared.qdrant_client import (
            get_qdrant_manager,
        )
        m1 = get_qdrant_manager()
        m2 = get_qdrant_manager()
        assert m1 is m2

    def test_initial_state_unconnected(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        assert mgr._http is None
        assert mgr.url == ""


class TestConnectDisconnect:
    """connect() and disconnect()."""

    @pytest.mark.asyncio
    async def test_connect_success(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_http.aclose = AsyncMock()

        with patch("aigateway_core.shared.qdrant_client.AsyncClient", return_value=mock_http):
            await mgr.connect("http://qdrant:6333")

        assert mgr.url == "http://qdrant:6333"
        mock_http.get.assert_called_once_with("/")

    @pytest.mark.asyncio
    async def test_connect_trims_trailing_slash(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_http.aclose = AsyncMock()

        with patch("aigateway_core.shared.qdrant_client.AsyncClient", return_value=mock_http):
            await mgr.connect("http://qdrant:6333/")

        assert mgr.url == "http://qdrant:6333"

    @pytest.mark.asyncio
    async def test_connect_failure_closes_http(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(side_effect=Exception("connection refused"))
        mock_http.aclose = AsyncMock()

        with patch("aigateway_core.shared.qdrant_client.AsyncClient", return_value=mock_http):
            with pytest.raises(ConnectionError, match="连接失败"):
                await mgr.connect("http://qdrant:6333")

        mock_http.aclose.assert_called_once()
        assert mgr._http is None

    @pytest.mark.asyncio
    async def test_disconnect_closes_http(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_http.aclose = AsyncMock()
        mgr._http = mock_http

        await mgr.disconnect()

        mock_http.aclose.assert_called_once()
        assert mgr._http is None

    @pytest.mark.asyncio
    async def test_disconnect_noop_when_not_connected(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        await mgr.disconnect()


class TestHeaders:
    """_headers() and _api_key_from_env()."""

    def test_headers_without_api_key(self, monkeypatch):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        monkeypatch.delenv("QDRANT_API_KEY", raising=False)
        headers = mgr._headers()
        assert headers == {"Content-Type": "application/json"}
        assert "api-key" not in headers

    def test_headers_with_api_key(self, monkeypatch):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        monkeypatch.setenv("QDRANT_API_KEY", "secret-key-123")
        headers = mgr._headers()
        assert headers["api-key"] == "secret-key-123"

    def test_api_key_from_env_none(self, monkeypatch):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        monkeypatch.delenv("QDRANT_API_KEY", raising=False)
        result = QdrantClientManager._api_key_from_env()
        assert result is None

    def test_api_key_from_env_present(self, monkeypatch):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        monkeypatch.setenv("QDRANT_API_KEY", "my-api-key")
        result = QdrantClientManager._api_key_from_env()
        assert result == "my-api-key"


class TestUpsertCollection:
    """upsert_collection()."""

    @pytest.mark.asyncio
    async def test_collection_already_exists(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": {"collections": [{"name": "semantic_cache"}]}}
        mock_http.get = AsyncMock(return_value=mock_resp)
        mgr._http = mock_http

        result = await mgr.upsert_collection("semantic_cache")
        assert result is True
        mock_http.get.assert_called_once_with("/collections/")

    @pytest.mark.asyncio
    async def test_collection_created(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": {"collections": []}}
        mock_http.get = AsyncMock(return_value=mock_resp)

        mock_put_resp = MagicMock()
        mock_put_resp.raise_for_status = MagicMock()
        mock_put_resp.json.return_value = {"result": True}
        mock_http.put = AsyncMock(return_value=mock_put_resp)
        mgr._http = mock_http

        result = await mgr.upsert_collection("semantic_cache")
        assert result is True
        mock_http.put.assert_called_once()
        call_args = mock_http.put.call_args
        assert "/collections/semantic_cache" in str(call_args)
        payload = call_args.kwargs.get("json", call_args[1].get("json", {}))
        assert payload["vectors"]["size"] == 1024
        assert payload["vectors"]["distance"] == "Cosine"

    @pytest.mark.asyncio
    async def test_raises_when_not_connected(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        with pytest.raises(RuntimeError, match="尚未连接"):
            await mgr.upsert_collection("semantic_cache")


class TestStoreEmbedding:
    """store_embedding() with 404 auto-create path."""
