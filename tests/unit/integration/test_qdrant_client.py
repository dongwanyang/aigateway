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
        assert mgr.url == "http://localhost:6333"


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
        await mgr.disconnect()  # should not raise


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

    @pytest.mark.asyncio
    async def test_store_embedding_success(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"result": {"operation": "complete"}}
        mock_http.put = AsyncMock(return_value=mock_resp)
        mgr._http = mock_http

        point_id = await mgr.store_embedding(
            "semantic_cache",
            {"prompt_hash": "abc"},
            [0.1, 0.2, 0.3],
        )
        assert isinstance(point_id, str)
        assert len(point_id) > 0
        mock_http.put.assert_called_once()

    @pytest.mark.asyncio
    async def test_store_embedding_auto_creates_on_404(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()

        # upsert_collection calls GET /collections/ first
        collections_resp = MagicMock()
        collections_resp.json.return_value = {"result": {"collections": []}}
        mock_http.get = AsyncMock(return_value=collections_resp)

        # Sequence of HTTP calls:
        # 1. PUT /collections/.../points → 404
        # 2. GET /collections/ → empty (for upsert_collection)
        # 3. PUT /collections/... → create collection (success)
        # 4. PUT /collections/.../points → success
        first_put = MagicMock()
        first_put.status_code = 404
        first_put.raise_for_status = MagicMock()
        first_put.json.return_value = {"result": {}}

        create_resp = MagicMock()
        create_resp.raise_for_status = MagicMock()
        create_resp.json.return_value = {"result": True}

        success_resp = MagicMock()
        success_resp.raise_for_status = MagicMock()
        success_resp.json.return_value = {"result": {"operation": "complete"}}

        mock_http.put = AsyncMock(side_effect=[first_put, create_resp, success_resp])
        mgr._http = mock_http

        await mgr.store_embedding(
            "semantic_cache",
            {"prompt_hash": "abc"},
            [0.1, 0.2, 0.3],
        )
        assert mock_http.put.call_count == 3  # 404 → create → retry put


class TestQueryVector:
    """query_vector() hit, miss, and 404 paths."""

    @pytest.mark.asyncio
    async def test_query_vector_hit(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "result": [{"id": "point-1", "score": 0.98, "payload": {"text": "hello"}}]
        }
        mock_http.post = AsyncMock(return_value=mock_resp)
        mgr._http = mock_http

        result = await mgr.query_vector("semantic_cache", [0.1, 0.2], limit=1)
        assert result is not None
        assert result["id"] == "point-1"
        assert result["score"] == 0.98

    @pytest.mark.asyncio
    async def test_query_vector_miss(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": []}
        mock_http.post = AsyncMock(return_value=mock_resp)
        mgr._http = mock_http

        result = await mgr.query_vector("semantic_cache", [0.1, 0.2])
        assert result is None

    @pytest.mark.asyncio
    async def test_query_vector_404_treated_as_miss(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status = MagicMock()
        mock_http.post = AsyncMock(return_value=mock_resp)
        mgr._http = mock_http

        result = await mgr.query_vector("nonexistent", [0.1, 0.2])
        assert result is None

    @pytest.mark.asyncio
    async def test_query_vector_with_user_filter(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": []}
        mock_http.post = AsyncMock(return_value=mock_resp)
        mgr._http = mock_http

        await mgr.query_vector("semantic_cache", [0.1], user_id="user-1")
        call_json = mock_http.post.call_args.kwargs["json"]
        assert "filter" in call_json
        assert call_json["filter"]["must"][0]["key"] == "user_id"

    @pytest.mark.asyncio
    async def test_query_vector_raises_when_not_connected(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        with pytest.raises(RuntimeError, match="尚未连接"):
            await mgr.query_vector("semantic_cache", [0.1])


class TestDeleteCollection:
    """delete_collection()."""

    @pytest.mark.asyncio
    async def test_delete_collection(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_http.delete = AsyncMock(return_value=mock_resp)
        mgr._http = mock_http

        result = await mgr.delete_collection("semantic_cache")
        assert result is True
        mock_http.delete.assert_called_once_with("/collections/semantic_cache")

    @pytest.mark.asyncio
    async def test_delete_collection_raises_when_not_connected(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        with pytest.raises(RuntimeError, match="尚未连接"):
            await mgr.delete_collection("semantic_cache")


class TestQueryVectorMulti:
    """query_vector_multi() — returns list of candidates."""

    @pytest.mark.asyncio
    async def test_returns_multiple_candidates(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "result": [
                {"id": "p1", "score": 0.98, "payload": {"text": "a"}},
                {"id": "p2", "score": 0.95, "payload": {"text": "b"}},
            ]
        }
        mock_http.post = AsyncMock(return_value=mock_resp)
        mgr._http = mock_http

        results = await mgr.query_vector_multi("semantic_cache", [0.1], limit=5)
        assert len(results) == 2
        assert results[0]["id"] == "p1"
        assert results[1]["score"] == 0.95

    @pytest.mark.asyncio
    async def test_empty_result(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": []}
        mock_http.post = AsyncMock(return_value=mock_resp)
        mgr._http = mock_http

        results = await mgr.query_vector_multi("semantic_cache", [0.1])
        assert results == []

    @pytest.mark.asyncio
    async def test_404_returns_none(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status = MagicMock()
        mock_http.post = AsyncMock(return_value=mock_resp)
        mgr._http = mock_http

        results = await mgr.query_vector_multi("nonexistent", [0.1])
        assert results is None

    @pytest.mark.asyncio
    async def test_query_vector_multi_with_user_filter(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": []}
        mock_http.post = AsyncMock(return_value=mock_resp)
        mgr._http = mock_http

        await mgr.query_vector_multi("semantic_cache", [0.1], user_id="user-1")
        call_json = mock_http.post.call_args.kwargs["json"]
        assert "filter" in call_json


class TestDeleteByFilter:
    """delete_by_filter()."""

    @pytest.mark.asyncio
    async def test_delete_by_filter(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"result": {"deleted_count": 3}}
        mock_http.post = AsyncMock(return_value=mock_resp)
        mgr._http = mock_http

        result = await mgr.delete_by_filter("semantic_cache", {"must": [{"key": "ttl", "lt": 100}]})
        assert result == 3

    @pytest.mark.asyncio
    async def test_delete_by_filter_dict_result(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"result": {"deleted_count": 5}}
        mock_http.post = AsyncMock(return_value=mock_resp)
        mgr._http = mock_http

        result = await mgr.delete_by_filter("semantic_cache", {})
        assert result == 5

    @pytest.mark.asyncio
    async def test_delete_by_filter_non_dict_result(self):
        """When result is not a dict, delete_by_filter returns 0."""
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"result": "some_string"}
        mock_http.post = AsyncMock(return_value=mock_resp)
        mgr._http = mock_http

        result = await mgr.delete_by_filter("semantic_cache", {})
        assert result == 0


class TestScrollPoints:
    """scroll_points()."""

    @pytest.mark.asyncio
    async def test_scroll_points_basic(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "result": {
                "points": [{"id": "p1", "payload": {"text": "a"}}],
                "next_page_offset": "abc",
            }
        }
        mock_http.post = AsyncMock(return_value=mock_resp)
        mgr._http = mock_http

        result = await mgr.scroll_points("semantic_cache", limit=10)
        assert len(result["points"]) == 1
        assert result["next_page_offset"] == "abc"

    @pytest.mark.asyncio
    async def test_scroll_points_with_filter_and_offset(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"result": {"points": [], "next_page_offset": None}}
        mock_http.post = AsyncMock(return_value=mock_resp)
        mgr._http = mock_http

        await mgr.scroll_points(
            "semantic_cache",
            filter={"must": [{"key": "user_id", "match": {"value": "u1"}}]},
            limit=5,
            offset="page2",
            with_payload=True,
        )
        call_json = mock_http.post.call_args.kwargs["json"]
        assert call_json["filter"]["must"][0]["key"] == "user_id"
        assert call_json["offset"] == "page2"
        assert call_json["limit"] == 5


class TestGetPoint:
    """get_point()."""

    @pytest.mark.asyncio
    async def test_get_point_found(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"result": {"id": "p1", "payload": {"text": "a"}}}
        mock_http.get = AsyncMock(return_value=mock_resp)
        mgr._http = mock_http

        result = await mgr.get_point("semantic_cache", "p1")
        assert result is not None
        assert result["id"] == "p1"

    @pytest.mark.asyncio
    async def test_get_point_not_found(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_http.get = AsyncMock(return_value=mock_resp)
        mgr._http = mock_http

        result = await mgr.get_point("semantic_cache", "missing")
        assert result is None


class TestUpdatePayload:
    """update_payload()."""

    @pytest.mark.asyncio
    async def test_update_payload(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_http.post = AsyncMock(return_value=mock_resp)
        mgr._http = mock_http

        result = await mgr.update_payload("semantic_cache", "p1", {"hit_count": 5})
        assert result is True


class TestDeletePoints:
    """delete_points()."""

    @pytest.mark.asyncio
    async def test_delete_points(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_http.post = AsyncMock(return_value=mock_resp)
        mgr._http = mock_http

        result = await mgr.delete_points("semantic_cache", ["p1", "p2"])
        assert result is True


class TestAllMethodsRaiseWhenNotConnected:
    """Every public method should raise RuntimeError when _http is None."""

    @pytest.mark.asyncio
    async def test_all_methods_raise(self):
        from aigateway_core.shared.qdrant_client import QdrantClientManager
        mgr = QdrantClientManager()
        methods_to_test = [
            ("upsert_collection", ("test",)),
            ("store_embedding", ("c", {}, [0.1])),
            ("query_vector", ("c", [0.1])),
            ("delete_collection", ("c",)),
            ("query_vector_multi", ("c", [0.1])),
            ("delete_by_filter", ("c", {})),
            ("scroll_points", ("c",)),
            ("get_point", ("c", "p")),
            ("update_payload", ("c", "p", {})),
            ("delete_points", ("c", ["p"])),
        ]
        for method_name, args in methods_to_test:
            method = getattr(mgr, method_name)
            try:
                await method(*args)
            except RuntimeError as e:
                assert "尚未连接" in str(e), f"{method_name} should raise RuntimeError with '尚未连接'"
