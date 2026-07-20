"""
Task Tracker — 异步任务持久化层
================================

管理聊天窗口中异步任务（视频生成）的状态追踪。

设计原则：
- 任务 ID 由上游 API（OpenAI Videos API）生成，本模块只负责映射和轮询
- 存储使用 Redis hash，key 格式：aigateway:task:{task_type}:{task_id}
- 过期时间 24 小时，与草稿 TTL 对齐
- 前端通过 GET /admin/chat/tasks 查询未完成任务列表

与 DraftGenerator 的区别：
- DraftGenerator 管理草稿工作流（生成/确认/拒绝/重生成）
- TaskTracker 管理纯异步任务（视频生成），由外部 API 驱动状态
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Redis key prefix for task tracking
_TASK_KEY_PREFIX = "aigateway:task"

# Default TTL: 24 hours
_DEFAULT_TTL_SECONDS = 24 * 3600


class TaskTracker:
    """异步任务追踪器。

    追踪视频生成等异步任务的当前状态，支持：
    - 注册新任务
    - 查询任务状态
    - 列出所有活跃任务
    - 清理已完成/过期的任务
    """

    def __init__(self, redis_client: Optional[Any] = None):
        """初始化 TaskTracker。

        Args:
            redis_client: Redis 客户端实例。若为 None，则使用内存字典模拟（测试用）。
        """
        self._redis_client = redis_client
        self._memory_store: Dict[str, str] = {}

    def _make_key(self, task_type: str, task_id: str) -> str:
        """构建 Redis 键名。

        Args:
            task_type: 任务类型（'video' | 'draft'）
            task_id: 任务唯一标识符

        Returns:
            Redis 键名
        """
        return f"{_TASK_KEY_PREFIX}:{task_type}:{task_id}"

    async def register(
        self,
        task_type: str,
        task_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        """注册新任务。

        Args:
            task_type: 任务类型（'video' | 'draft'）
            task_id: 任务唯一标识符
            metadata: 附加元数据（如 prompt、model、session_id 等）
            ttl_seconds: 过期时间（秒），默认 24 小时
        """
        now = time.time()
        data = {
            "task_type": task_type,
            "task_id": task_id,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            "metadata": metadata or {},
        }

        key = self._make_key(task_type, task_id)

        if self._redis_client is not None:
            await self._redis_client.set(key, json.dumps(data), ex=ttl_seconds)
        else:
            self._memory_store[key] = json.dumps(data)

        logger.info(
            "task_tracker.registered",
            extra={
                "task_type": task_type,
                "task_id": task_id,
                "ttl_seconds": ttl_seconds,
            },
        )

    async def get_status(self, task_type: str, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务状态。

        Args:
            task_type: 任务类型
            task_id: 任务唯一标识符

        Returns:
            任务数据 dict，不存在或已过期返回 None
        """
        key = self._make_key(task_type, task_id)

        if self._redis_client is not None:
            raw = await self._redis_client.get(key)
        else:
            raw = self._memory_store.get(key)

        if raw is None:
            return None

        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")

        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.error(
                "task_tracker.deserialize_error",
                extra={"task_type": task_type, "task_id": task_id},
            )
            return None

    async def update_status(
        self,
        task_type: str,
        task_id: str,
        status: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """更新任务状态。

        Args:
            task_type: 任务类型
            task_id: 任务唯一标识符
            status: 新状态（'pending' | 'in_progress' | 'succeeded' | 'failed' | 'expired'）
            metadata: 可选的元数据更新

        Returns:
            True 如果更新成功，False 如果任务不存在
        """
        existing = await self.get_status(task_type, task_id)
        if existing is None:
            return False

        existing["status"] = status
        existing["updated_at"] = time.time()
        if metadata:
            existing["metadata"].update(metadata)

        key = self._make_key(task_type, task_id)

        # 计算剩余 TTL
        ttl = None
        if self._redis_client is not None:
            ttl = await self._redis_client.ttl(key)
            if ttl and ttl > 0:
                await self._redis_client.set(key, json.dumps(existing), ex=ttl)
            else:
                # TTL 已过期或未知，重新设置
                await self._redis_client.set(key, json.dumps(existing), ex=_DEFAULT_TTL_SECONDS)
        else:
            self._memory_store[key] = json.dumps(existing)

        logger.info(
            "task_tracker.updated",
            extra={
                "task_type": task_type,
                "task_id": task_id,
                "status": status,
            },
        )

        return True

    async def list_active(self, task_type: Optional[str] = None) -> list[Dict[str, Any]]:
        """列出所有活跃任务。

        Args:
            task_type: 可选的任务类型过滤器

        Returns:
            活跃任务列表
        """
        # 扫描所有匹配前缀的 key
        pattern = f"{_TASK_KEY_PREFIX}:{task_type}:*" if task_type else f"{_TASK_KEY_PREFIX}:*:*"

        if self._redis_client is not None:
            keys = await self._redis_client.keys(pattern)
            tasks = []
            for key in keys:
                raw = await self._redis_client.get(key)
                if raw:
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    try:
                        tasks.append(json.loads(raw))
                    except (json.JSONDecodeError, TypeError):
                        continue
            return tasks
        else:
            # 内存模式：解析 JSON 后按 task_type 字段过滤。
            # 不能用 `task_type in v`(v 是 JSON 字符串)——那是子串匹配,
            # 会把 metadata 里提到 "video" 的 draft 任务误归入 video 过滤结果。
            result: list[Dict[str, Any]] = []
            for v in self._memory_store.values():
                try:
                    t = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    continue
                if task_type is None or t.get("task_type") == task_type:
                    result.append(t)
            return result

    async def delete(self, task_type: str, task_id: str) -> None:
        """删除任务记录。

        Args:
            task_type: 任务类型
            task_id: 任务唯一标识符
        """
        key = self._make_key(task_type, task_id)

        if self._redis_client is not None:
            await self._redis_client.delete(key)
        else:
            self._memory_store.pop(key, None)

        logger.debug(
            "task_tracker.deleted",
            extra={
                "task_type": task_type,
                "task_id": task_id,
            },
        )
