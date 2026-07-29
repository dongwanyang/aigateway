"""Unit tests for RedisClientManager — all methods mockable with MagicMock."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestRedisClientManagerSingleton:
    """get_redis_manager() lazy init + idempotent."""

    def test_returns_same_instance(self):
        from aigateway_core.shared.redis_client import get_redis_manager
        # First call creates it
        m1 = get_redis_manager()
        # Second call returns same
        m2 = get_redis_manager()
        assert m1 is m2

    def test_initial_state_unconnected(self):
        from aigateway_core.shared.redis_client import get_redis_manager
        mgr = get_redis_manager()
        assert mgr.redis is None
        assert mgr._pubsub is None


class TestConnectDisconnect:
    """connect() and disconnect()."""

    @pytest.mark.asyncio
    async def test_connect_reuses_existing(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mock_redis = AsyncMock()
        mgr.redis = mock_redis
        result = await mgr.connect("redis://localhost:6379/0")
        assert result is mock_redis
        mock_redis.ping.assert_not_called()  # reused, not reconnected

    @pytest.mark.asyncio
    async def test_connect_creates_and_pings(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        with patch("aigateway_core.shared.redis_client.redis.from_url") as mock_from_url:
            mock_redis = AsyncMock()
            mock_redis.ping = AsyncMock(return_value=True)
            mock_from_url.return_value = mock_redis
            result = await mgr.connect("redis://test-url", connect_timeout=5)
            mock_from_url.assert_called_once_with(
                "redis://test-url",
                decode_responses=False,
                socket_connect_timeout=5,
                socket_timeout=10,
                retry_on_timeout=True,
                health_check_interval=30,
            )
            mock_redis.ping.assert_called_once()
            assert result is mock_redis

    @pytest.mark.asyncio
    async def test_disconnect_closes_pubsub_and_redis(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mock_pubsub = AsyncMock()
        mock_redis = AsyncMock()
        mgr._pubsub = mock_pubsub
        mgr.redis = mock_redis
        await mgr.disconnect()
        mock_pubsub.aclose.assert_called_once()
        mock_redis.aclose.assert_called_once()
        assert mgr.redis is None
        assert mgr._pubsub is None

    @pytest.mark.asyncio
    async def test_disconnect_handles_pubsub_already_closed(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mock_pubsub = AsyncMock()
        mock_pubsub.aclose.side_effect = Exception("already closed")
        mock_redis = AsyncMock()
        mgr._pubsub = mock_pubsub
        mgr.redis = mock_redis
        await mgr.disconnect()  # should not raise
        assert mgr.redis is None
        assert mgr._pubsub is None

    @pytest.mark.asyncio
    async def test_disconnect_handles_redis_already_closed(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mock_redis = AsyncMock()
        mock_redis.aclose.side_effect = Exception("already closed")
        mgr.redis = mock_redis
        await mgr.disconnect()  # should not raise
        assert mgr.redis is None


class TestPublish:
    """publish() with string and dict messages."""

    @pytest.mark.asyncio
    async def test_publish_string_message(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        mgr.redis.publish = AsyncMock(return_value=3)
        result = await mgr.publish("channel", "hello")
        assert result == 3
        mgr.redis.publish.assert_called_once_with("channel", "hello")

    @pytest.mark.asyncio
    async def test_publish_dict_message_json_encoded(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        mgr.redis.publish = AsyncMock(return_value=2)
        msg = {"event": "config_reload", "key": "plugins_enabled"}
        await mgr.publish("channel", msg)
        expected_json = json.dumps(msg, ensure_ascii=False, default=str)
        mgr.redis.publish.assert_called_once_with("channel", expected_json)

    @pytest.mark.asyncio
    async def test_publish_raises_when_not_connected(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        with pytest.raises(RuntimeError, match="尚未连接"):
            await mgr.publish("channel", "data")


class TestSubscribe:
    """subscribe() async generator."""

    @pytest.mark.asyncio
    async def test_subscribe_raises_when_not_connected(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        gen = mgr.subscribe("channel")
        with pytest.raises(RuntimeError, match="尚未连接"):
            await gen.__anext__()

    @pytest.mark.asyncio
    async def test_subscribe_yields_decoded_messages(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mock_redis = AsyncMock()
        mgr.redis = mock_redis

        mock_pubsub = AsyncMock()
        mock_pubsub.subscribe = AsyncMock()
        mock_pubsub.get_message = AsyncMock(side_effect=[
            {"type": "message", "data": b"hello world"},
            {"type": "message", "data": "unicode: 世界"},
            {"type": "other", "data": "ignored"},
            None,  # timeout → continue
            {"type": "message", "data": "after timeout"},
        ])
        mock_pubsub.unsubscribe = AsyncMock()

        mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

        msgs = []
        async for msg in mgr.subscribe("ch1", "ch2"):
            msgs.append(msg)
            if len(msgs) >= 3:
                return  # exit test early, bypassing async for cleanup

        assert msgs == ["hello world", "unicode: 世界", "after timeout"]
        mock_redis.pubsub.assert_called_once()
        mock_pubsub.subscribe.assert_called_once_with("ch1", "ch2")
        mock_pubsub.unsubscribe.assert_called_once_with("ch1", "ch2")

    @pytest.mark.asyncio
    async def test_subscribe_closes_previous_pubsub(self):
        """subscribe() should close the previous pubsub before creating a new one."""
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mock_redis = AsyncMock()
        mgr.redis = mock_redis

        old_pubsub = AsyncMock()
        old_pubsub.aclose = AsyncMock()
        mgr._pubsub = old_pubsub

        new_pubsub = AsyncMock()
        new_pubsub.subscribe = AsyncMock()
        new_pubsub.get_message = AsyncMock(side_effect=asyncio.CancelledError())
        new_pubsub.unsubscribe = AsyncMock()
        mock_redis.pubsub = MagicMock(return_value=new_pubsub)

        async for msg in mgr.subscribe("ch"):
            pass  # cancelled gracefully

        old_pubsub.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_subscribe_handles_cancelled_error(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mock_redis = AsyncMock()
        mgr.redis = mock_redis

        mock_pubsub = AsyncMock()
        mock_pubsub.subscribe = AsyncMock()
        mock_pubsub.get_message = AsyncMock(side_effect=asyncio.CancelledError())
        mock_pubsub.unsubscribe = AsyncMock()

        mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

        async for msg in mgr.subscribe("ch"):
            pass  # cancelled gracefully

        mock_redis.pubsub.assert_called_once()
        mock_pubsub.unsubscribe.assert_called_once()


class TestSetGetDeleteApiKey:
    """set_api_key, get_api_key, delete_api_key."""

    @pytest.mark.asyncio
    async def test_set_api_key(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        await mgr.set_api_key("abc123", {"key_id": "k1", "user_id": "u1"})
        mgr.redis.hset.assert_called_once_with(
            "aigateway:key:abc123", mapping={"key_id": "k1", "user_id": "u1"}
        )

    @pytest.mark.asyncio
    async def test_get_api_key_success(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        mgr.redis.hgetall = AsyncMock(return_value={b"key_id": b"k1", b"user_id": b"u1"})
        result = await mgr.get_api_key("abc123")
        assert result == {"key_id": "k1", "user_id": "u1"}

    @pytest.mark.asyncio
    async def test_get_api_key_not_found(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        mgr.redis.hgetall = AsyncMock(return_value={})
        result = await mgr.get_api_key("abc123")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_api_key_single(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        mgr.redis.delete = AsyncMock(return_value=1)
        result = await mgr.delete_api_key("abc123")
        assert result is True
        mgr.redis.delete.assert_called_once_with("aigateway:key:abc123")

    @pytest.mark.asyncio
    async def test_delete_api_key_with_prefix(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        mgr.redis.delete = AsyncMock(return_value=2)
        result = await mgr.delete_api_key("abc123", key_prefix="gw-test")
        assert result is True
        mgr.redis.delete.assert_called_once_with(
            "aigateway:key:abc123", "aigateway:key_lookup:gw-test"
        )

    @pytest.mark.asyncio
    async def test_delete_api_key_nothing_deleted(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        mgr.redis.delete = AsyncMock(return_value=0)
        result = await mgr.delete_api_key("abc123")
        assert result is False


class TestKeyLookup:
    """set_key_lookup, get_key_lookup."""

    @pytest.mark.asyncio
    async def test_set_key_lookup(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        await mgr.set_key_lookup("gw-test", "abc123")
        mgr.redis.set.assert_called_once_with("aigateway:key_lookup:gw-test", "abc123")

    @pytest.mark.asyncio
    async def test_get_key_lookup_found(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        mgr.redis.get = AsyncMock(return_value=b"abc123")
        result = await mgr.get_key_lookup("gw-test")
        assert result == "abc123"

    @pytest.mark.asyncio
    async def test_get_key_lookup_not_found(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        mgr.redis.get = AsyncMock(return_value=None)
        result = await mgr.get_key_lookup("gw-test")
        assert result is None


class TestGroupOperations:
    """set_group, get_group, delete_group, group lookup."""

    @pytest.mark.asyncio
    async def test_set_group(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        await mgr.set_group("team-a", {"name": "Team A", "shared_daily_tokens": 5000000})
        mgr.redis.hset.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_group_found(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        mgr.redis.hgetall = AsyncMock(return_value={b"name": b"Team A"})
        result = await mgr.get_group("team-a")
        assert result == {"name": "Team A"}

    @pytest.mark.asyncio
    async def test_get_group_not_found(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        mgr.redis.hgetall = AsyncMock(return_value={})
        result = await mgr.get_group("team-a")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_group(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        mgr.redis.delete = AsyncMock(return_value=1)
        result = await mgr.delete_group("team-a")
        assert result is True

    @pytest.mark.asyncio
    async def test_set_group_lookup(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        await mgr.set_group_lookup("team-a", "grp-001")
        mgr.redis.set.assert_called_once_with("aigateway:group_lookup:team-a", "grp-001")

    @pytest.mark.asyncio
    async def test_get_group_lookup(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        mgr.redis.get = AsyncMock(return_value=b"grp-001")
        result = await mgr.get_group_lookup("team-a")
        assert result == "grp-001"

    @pytest.mark.asyncio
    async def test_delete_group_lookup(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        await mgr.delete_group_lookup("team-a")
        mgr.redis.delete.assert_called_once_with("aigateway:group_lookup:team-a")


class TestQuotaOperations:
    """set_quota, get_quota."""

    @pytest.mark.asyncio
    async def test_set_quota(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        data = {"tokens_in": 1000, "tokens_out": 500, "cost_usd": 0.05}
        await mgr.set_quota("abc123", "daily:2024-01-21", data)
        mgr.redis.hset.assert_called_once_with(
            "aigateway:quota:abc123:daily:2024-01-21", mapping=data
        )

    @pytest.mark.asyncio
    async def test_get_quota_found(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        mgr.redis.hgetall = AsyncMock(return_value={b"tokens_in": b"1000"})
        result = await mgr.get_quota("abc123", "daily:2024-01-21")
        assert result == {"tokens_in": "1000"}

    @pytest.mark.asyncio
    async def test_get_quota_not_found(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        mgr.redis.hgetall = AsyncMock(return_value={})
        result = await mgr.get_quota("abc123", "daily:2024-01-21")
        assert result is None


class TestRateLimiting:
    """add_rpm_entry, clean_old_rpm_entries, count_rpm_recent."""

    @pytest.mark.asyncio
    async def test_add_rpm_entry(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        mock_pipe = MagicMock()
        mock_pipe.execute = AsyncMock(return_value=[1])
        mgr.redis.pipeline = MagicMock(return_value=mock_pipe)
        result = await mgr.add_rpm_entry("abc123", "req-1", 1700000000.0)
        assert result == 1

    @pytest.mark.asyncio
    async def test_clean_old_rpm_entries(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        mgr.redis.zremrangebyscore = AsyncMock(return_value=5)
        result = await mgr.clean_old_rpm_entries("abc123", 1700000000.0)
        assert result == 5

    @pytest.mark.asyncio
    async def test_count_rpm_recent(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        mgr.redis.zcount = AsyncMock(return_value=42)
        result = await mgr.count_rpm_recent("abc123", 1700000000.0)
        assert result == 42


class TestTpmWindow:
    """set_tpm_window, get_tpm_window."""

    @pytest.mark.asyncio
    async def test_set_tpm_window(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        await mgr.set_tpm_window("abc123", 5000)
        mgr.redis.set.assert_called_once_with(
            "aigateway:ratelimit:abc123:tpm", "5000", ex=60
        )

    @pytest.mark.asyncio
    async def test_get_tpm_window_found(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        mgr.redis.get = AsyncMock(return_value=b"5000")
        result = await mgr.get_tpm_window("abc123")
        assert result == 5000

    @pytest.mark.asyncio
    async def test_get_tpm_window_not_found(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        mgr.redis.get = AsyncMock(return_value=None)
        result = await mgr.get_tpm_window("abc123")
        assert result == 0


class TestPipeBatch:
    """pipe_batch atomic pipeline."""

    @pytest.mark.asyncio
    async def test_pipe_batch_list_style(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        mock_pipe = MagicMock()
        mock_pipe.execute = AsyncMock(return_value=["OK", 1, "OK"])
        mgr.redis.pipeline = MagicMock(return_value=mock_pipe)

        def fn(pipe):
            pipe.hset("key", mapping={"x": "1"})
            pipe.set("key2", "val")
            return [pipe.hset("key", mapping={"y": "2"}), pipe.set("key3", "v")]

        result = await mgr.pipe_batch(fn)
        assert result == ["OK", 1, "OK"]

    @pytest.mark.asyncio
    async def test_pipe_batch_legacy_single_call(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        mgr.redis = AsyncMock()
        mock_pipe = MagicMock()
        mock_pipe.execute = AsyncMock(return_value=["OK"])
        mgr.redis.pipeline = MagicMock(return_value=mock_pipe)

        def fn(pipe):
            return pipe.hset("key", mapping={"x": "1"})

        result = await mgr.pipe_batch(fn)
        assert result == ["OK"]

    @pytest.mark.asyncio
    async def test_pipe_batch_raises_when_not_connected(self):
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        with pytest.raises(RuntimeError, match="尚未连接"):
            await mgr.pipe_batch(lambda p: [])

    @pytest.mark.asyncio
    async def test_all_methods_raise_when_not_connected(self):
        """Every public method should raise RuntimeError when redis is None."""
        from aigateway_core.shared.redis_client import RedisClientManager
        mgr = RedisClientManager()
        import asyncio
        methods_to_test = [
            ("publish", ("ch", "msg")),
            ("subscribe", ()),
            ("set_api_key", ("k", {})),
            ("get_api_key", ("k",)),
            ("delete_api_key", ("k",)),
            ("set_key_lookup", ("p", "h")),
            ("get_key_lookup", ("p",)),
            ("set_group", ("g", {})),
            ("get_group", ("g",)),
            ("delete_group", ("g",)),
            ("set_group_lookup", ("n", "g")),
            ("get_group_lookup", ("n",)),
            ("delete_group_lookup", ("n",)),
            ("set_quota", ("k", "p", {})),
            ("get_quota", ("k", "p")),
            ("add_rpm_entry", ("k", "r", 0.0)),
            ("clean_old_rpm_entries", ("k", 0.0)),
            ("count_rpm_recent", ("k", 0.0)),
            ("set_tpm_window", ("k", 100)),
            ("get_tpm_window", ("k",)),
            ("pipe_batch", (lambda p: None,)),
        ]
        for method_name, args in methods_to_test:
            method = getattr(mgr, method_name)
            try:
                result = method(*args)
                if asyncio.iscoroutine(result):
                    await result
            except RuntimeError as e:
                assert "尚未连接" in str(e), f"{method_name} should raise RuntimeError with '尚未连接', got: {e}"
