"""TaskTracker 单元测试

覆盖 TaskTracker 的核心功能：注册、查询、更新状态、列出活跃任务、删除。
"""

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway_api", "src"))

from aigateway_api.task_tracker import TaskTracker


# ==================================================================
# Helper
# ==================================================================


def _memory_tracker():
    """创建使用内存存储的 TaskTracker（无需 Redis）。"""
    return TaskTracker(redis_client=None)


def _redis_tracker():
    """创建使用 Mock Redis 的 TaskTracker。"""
    redis_client = MagicMock()
    redis_client.set = AsyncMock()
    redis_client.get = AsyncMock()
    redis_client.delete = AsyncMock()
    redis_client.ttl = AsyncMock(return_value=3600)
    redis_client.scan = AsyncMock(return_value=("0", []))
    return TaskTracker(redis_client=redis_client), redis_client


# ==================================================================
# register Tests
# ==================================================================


class TestRegister:
    """测试注册新任务。"""

    @pytest.mark.asyncio
    async def test_register_memory(self):
        """内存模式下注册任务。"""
        tracker = _memory_tracker()
        await tracker.register("video", "vid-123", {"prompt": "test"})
        status = await tracker.get_status("video", "vid-123")
        assert status is not None
        assert status["task_type"] == "video"
        assert status["task_id"] == "vid-123"
        assert status["status"] == "pending"
        assert status["metadata"]["prompt"] == "test"

    @pytest.mark.asyncio
    async def test_register_redis(self):
        """Redis 模式下注册任务。"""
        tracker, redis = _redis_tracker()
        await tracker.register("video", "vid-456", {"model": "agnes-video"})
        redis.set.assert_called_once()
        call_args = redis.set.call_args
        key = call_args.args[0]
        assert key.startswith("aigateway:task:video:")
        data = json.loads(call_args.args[1])
        assert data["task_type"] == "video"
        assert data["status"] == "pending"

    @pytest.mark.asyncio
    async def test_register_with_ttl(self):
        """自定义 TTL 通过 ex 参数传递（断言 ex 值，而非仅调用次数）。"""
        tracker, redis = _redis_tracker()
        await tracker.register("video", "vid-ttl", ttl_seconds=7200)
        redis.set.assert_called_once()
        assert redis.set.call_args.kwargs["ex"] == 7200

    @pytest.mark.asyncio
    async def test_register_default_metadata(self):
        """默认 metadata 为空字典。"""
        tracker = _memory_tracker()
        await tracker.register("draft", "d-1")
        status = await tracker.get_status("draft", "d-1")
        assert status["metadata"] == {}


# ==================================================================
# get_status Tests
# ==================================================================


class TestGetStatus:
    """测试获取任务状态。"""

    @pytest.mark.asyncio
    async def test_get_nonexistent(self):
        """不存在的任务返回 None。"""
        tracker = _memory_tracker()
        result = await tracker.get_status("video", "nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_after_register(self):
        """注册后能获取到状态。"""
        tracker = _memory_tracker()
        await tracker.register("video", "v1", {"key": "value"})
        status = await tracker.get_status("video", "v1")
        assert status is not None
        assert status["task_id"] == "v1"

    @pytest.mark.asyncio
    async def test_get_corrupted_json(self):
        """损坏的 JSON 数据返回 None。"""
        tracker = _memory_tracker()
        tracker._memory_store["aigateway:task:video:bad"] = "not json"
        result = await tracker.get_status("video", "bad")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_bytes_data(self):
        """bytes 数据能正确解码。"""
        tracker = _memory_tracker()
        data = {"task_type": "video", "task_id": "b1", "status": "pending"}
        tracker._memory_store["aigateway:task:video:b1"] = json.dumps(data).encode("utf-8")
        result = await tracker.get_status("video", "b1")
        assert result is not None
        assert result["task_type"] == "video"


# ==================================================================
# update_status Tests
# ==================================================================


class TestUpdateStatus:
    """测试更新任务状态。"""

    @pytest.mark.asyncio
    async def test_update_existing(self):
        """更新已存在的任务。"""
        tracker = _memory_tracker()
        await tracker.register("video", "v1")
        success = await tracker.update_status("video", "v1", "in_progress")
        assert success is True
        status = await tracker.get_status("video", "v1")
        assert status["status"] == "in_progress"

    @pytest.mark.asyncio
    async def test_update_nonexistent(self):
        """更新不存在的任务返回 False。"""
        tracker = _memory_tracker()
        success = await tracker.update_status("video", "nonexistent", "succeeded")
        assert success is False

    @pytest.mark.asyncio
    async def test_update_with_metadata(self):
        """更新时合并 metadata。"""
        tracker = _memory_tracker()
        await tracker.register("video", "v1", {"original": "data"})
        await tracker.update_status("video", "v1", "succeeded", {"result": "ok"})
        status = await tracker.get_status("video", "v1")
        assert status["metadata"]["original"] == "data"
        assert status["metadata"]["result"] == "ok"

    @pytest.mark.asyncio
    async def test_update_redis_preserves_ttl(self):
        """Redis 模式下更新时查询剩余 TTL 并在 set 时保留（ex=）。"""
        tracker, redis = _redis_tracker()
        await tracker.register("video", "v1")
        redis.get.return_value = json.dumps({
            "task_type": "video",
            "task_id": "v1",
            "status": "pending",
            "created_at": 1,
            "updated_at": 1,
            "metadata": {},
        })
        # 模拟剩余 TTL=3600s
        redis.ttl = AsyncMock(return_value=3600)
        success = await tracker.update_status("video", "v1", "succeeded")
        assert success is True
        # 更新路径应查询 ttl，并在 set 时用该 ttl 作 ex
        redis.ttl.assert_called()
        last_set = redis.set.call_args_list[-1]
        assert last_set.kwargs["ex"] == 3600


# ==================================================================
# list_active Tests
# ==================================================================


class TestListActive:
    """测试列出活跃任务。"""

    @pytest.mark.asyncio
    async def test_list_empty(self):
        """空列表。"""
        tracker = _memory_tracker()
        tasks = await tracker.list_active()
        assert tasks == []

    @pytest.mark.asyncio
    async def test_list_all(self):
        """列出所有任务。"""
        tracker = _memory_tracker()
        await tracker.register("video", "v1")
        await tracker.register("draft", "d1")
        tasks = await tracker.list_active()
        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_list_filter_by_type(self):
        """按类型过滤。"""
        tracker = _memory_tracker()
        await tracker.register("video", "v1")
        await tracker.register("video", "v2")
        await tracker.register("draft", "d1")
        tasks = await tracker.list_active(task_type="video")
        assert len(tasks) == 2
        assert all(t["task_type"] == "video" for t in tasks)

    @pytest.mark.asyncio
    async def test_list_memory_no_substring_match(self):
        """内存模式过滤不应匹配 metadata 中的子串。"""
        tracker = _memory_tracker()
        await tracker.register("draft", "d1", {"metadata": "contains video word"})
        tasks = await tracker.list_active(task_type="video")
        assert len(tasks) == 0

    @pytest.mark.asyncio
    async def test_list_corrupted_entries_skipped(self):
        """损坏的条目被跳过。"""
        tracker = _memory_tracker()
        await tracker.register("video", "v1")
        tracker._memory_store["aigateway:task:video:bad"] = "corrupted"
        tasks = await tracker.list_active()
        assert len(tasks) == 1

    @pytest.mark.asyncio
    async def test_list_redis_scan(self):
        """Redis 模式使用 SCAN。"""
        tracker, redis = _redis_tracker()
        redis.scan = AsyncMock(return_value=("0", ["key1", "key2"]))
        redis.get = AsyncMock(return_value=json.dumps({
            "task_type": "video",
            "task_id": "t1",
            "status": "pending",
            "created_at": 1,
            "updated_at": 1,
            "metadata": {},
        }))
        tasks = await tracker.list_active()
        assert len(tasks) == 2  # scan returns 2 keys, both get same data


# ==================================================================
# delete Tests
# ==================================================================


class TestDelete:
    """测试删除任务。"""

    @pytest.mark.asyncio
    async def test_delete_memory(self):
        """内存模式下删除任务。"""
        tracker = _memory_tracker()
        await tracker.register("video", "v1")
        await tracker.delete("video", "v1")
        status = await tracker.get_status("video", "v1")
        assert status is None

    @pytest.mark.asyncio
    async def test_delete_redis(self):
        """Redis 模式下删除任务。"""
        tracker, redis = _redis_tracker()
        await tracker.delete("video", "v1")
        redis.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self):
        """删除不存在的任务不报错。"""
        tracker = _memory_tracker()
        await tracker.delete("video", "nonexistent")
        # 无异常即通过


# ==================================================================
# Key Format Tests
# ==================================================================


class TestKeyFormat:
    """测试 Redis 键名格式。"""

    def test_make_key_video(self):
        tracker = _memory_tracker()
        key = tracker._make_key("video", "vid-123")
        assert key == "aigateway:task:video:vid-123"

    def test_make_key_draft(self):
        tracker = _memory_tracker()
        key = tracker._make_key("draft", "d-456")
        assert key == "aigateway:task:draft:d-456"
