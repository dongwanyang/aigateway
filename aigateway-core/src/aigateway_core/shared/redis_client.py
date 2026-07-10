"""
Redis 连接管理
==============

提供 Redis 连接池、Pub/Sub 发布的异步封装。
基于 redis-py 的 hiredis 驱动，支持连接复用。

根据 DB_SCHEMA.md 第 4 节 — Pub/Sub 频道定义。
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import redis.asyncio as redis


class RedisClientManager:
    """Redis 连接管理器，负责连接池、Pub/Sub 频道收发。

    属性:
        url: Redis 连接地址，例如 "redis://localhost:6379/0"。
        redis: 底层 redis.asyncio.Redis 客户端实例。
        _pubsub: Pub/Sub 环形客户端，用于异步订阅。
    """

    def __init__(self) -> None:
        self.redis: redis.Redis | None = None
        self._pubsub: redis.client.PubSub | None = None

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    async def connect(
        self,
        url: str = "redis://localhost:6379/0",
        connect_timeout: int = 5,
        socket_timeout: int = 10,
        health_check_interval: int = 30,
    ) -> redis.Redis:
        """建立 Redis 连接池，启用 hiredis 驱动解析。

        Args:
            url: Redis 连接 URL。
            connect_timeout: 连接超时（秒），默认 5。
            socket_timeout: 套接字超时（秒），默认 10。
            health_check_interval: 健康检查间隔（秒），默认 30。

        Returns:
            redis.asyncio.Redis 客户端实例。

        Raises:
            ConnectionError: 连接失败时抛出。
        """
        if self.redis is not None:
            # 已连接则复用现有实例
            return self.redis

        self.redis = redis.from_url(
            url,
            decode_responses=False,  # 使用 bytes 以减少中间转换，部分场景下性能更好
            socket_connect_timeout=connect_timeout,
            socket_timeout=socket_timeout,
            retry_on_timeout=True,
            health_check_interval=health_check_interval,
            # hiredis 解析器：如果已安装则自动启用
        )
        # 验证连通性
        await self.redis.ping()
        return self.redis

    async def disconnect(self) -> None:
        """关闭所有连接及 Pub/Sub 订阅。"""
        if self._pubsub is not None:
            try:
                await self._pubsub.aclose()
            except Exception:
                pass
            self._pubsub = None

        if self.redis is not None:
            try:
                await self.redis.close()
            except Exception:
                pass
            self.redis = None

    # ------------------------------------------------------------------
    # Pub/Sub 发布
    # ------------------------------------------------------------------

    async def publish(self, channel: str, message: str | dict) -> int:
        """向指定频道发布消息。

        Args:
            channel: 频道名称，如 "aigateway:keys:sync"。
            message: 消息体，字符串或字典（会自动 JSON 序列化）。

        Returns:
            成功接收消息的客户端数量。
        """
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接，请先调用 connect()")

        if isinstance(message, dict):
            message = json.dumps(message, ensure_ascii=False, default=str)

        return await self.redis.publish(channel, message)

    # ------------------------------------------------------------------
    # Pub/Sub 订阅
    # ------------------------------------------------------------------

    async def subscribe(self, *channels: str) -> AsyncIterator[str]:
        """订阅一个或多个 Pub/Sub 频道，以异步生成器方式返回消息。

        Args:
            *channels: 要订阅的频道名称列表。

        Yields:
            订阅频道收到的消息字符串（已解码为 UTF-8）。

        Example:
            >>> async for msg in client.subscribe("aigateway:keys:sync"):
            ...     print(msg)
        """
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接，请先调用 connect()")

        # 取消之前的订阅
        if self._pubsub is not None:
            await self._pubsub.aclose()

        self._pubsub = self.redis.pubsub()
        await self._pubsub.subscribe(*channels)

        try:
            while True:
                message = await self._pubsub.get_message(
                    ignore_subscribe_events=True, timeout=1.0
                )
                if message is None:
                    continue
                if message["type"] == "message":
                    # 消息可能以 bytes 形式到达，需解码
                    data = message["data"]
                    if isinstance(data, bytes):
                        data = data.decode("utf-8", errors="replace")
                    yield data
        except asyncio.CancelledError:
            # 异步生成器被取消时优雅退出
            pass
        finally:
            if self._pubsub is not None:
                await self._pubsub.unsubscribe(*channels)

    # ------------------------------------------------------------------
    # 便捷操作 — API Key 存储 (DB_SCHEMA §1)
    # ------------------------------------------------------------------

    async def set_api_key(self, key_hash: str, data: dict) -> None:
        """写入 API Key 的 Hash 结构到 Redis。

        Key 格式: aigateway:key:{key_hash}

        Args:
            key_hash: API Key 的 SHA-256 哈希前 16 位 hex。
            data: 字段字典，包含 key_id, key_prefix, user_id, status 等。
        """
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        await self.redis.hset(
            f"aigateway:key:{key_hash}", mapping=data
        )

    async def get_api_key(self, key_hash: str) -> dict | None:
        """从 Redis 查询 API Key Hash 数据。

        Args:
            key_hash: API Key 的 SHA-256 哈希前 16 位 hex。

        Returns:
            字段字典，不存在时返回 None。
        """
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        raw = await self.redis.hgetall(f"aigateway:key:{key_hash}")
        if not raw:
            return None
        # redis-py hgetall 返回 bytes-keyed dict（decode_responses=False）
        return {k.decode(): v.decode() for k, v in raw.items()}

    async def delete_api_key(self, key_hash: str, key_prefix: str | None = None) -> bool:
        """删除 API Key 及其 lookup 记录。

        Args:
            key_hash: API Key 的 SHA-256 哈希前 16 位 hex。
            key_prefix: API Key 的前 8 字符，用于删除反向查找记录。

        Returns:
            是否成功删除至少一个键。
        """
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        keys_to_delete = [f"aigateway:key:{key_hash}"]
        if key_prefix:
            keys_to_delete.append(f"aigateway:key_lookup:{key_prefix}")
        return bool(await self.redis.delete(*keys_to_delete))

    async def set_key_lookup(self, key_prefix: str, key_hash: str) -> None:
        """写入 key_prefix -> key_hash 的反向查找映射。

        DB_SCHEMA §1 说明 Key 格式: aigateway:key_lookup:{key_prefix} -> key_hash
        """
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        await self.redis.set(
            f"aigateway:key_lookup:{key_prefix}", key_hash
        )

    async def get_key_lookup(self, key_prefix: str) -> str | None:
        """通过 key_prefix 反向查找 key_hash。"""
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        val = await self.redis.get(f"aigateway:key_lookup:{key_prefix}")
        return val.decode() if val else None

    # ------------------------------------------------------------------
    # 便捷操作 - 用户组存储 (GroupStore)
    # ------------------------------------------------------------------

    async def set_group(self, group_id: str, data: dict) -> None:
        """写入用户组 Hash。Key 格式: aigateway:group:{group_id}"""
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        await self.redis.hset(f"aigateway:group:{group_id}", mapping=data)

    async def get_group(self, group_id: str) -> dict | None:
        """读取用户组 Hash。"""
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        raw = await self.redis.hgetall(f"aigateway:group:{group_id}")
        if not raw:
            return None
        return {k.decode(): v.decode() for k, v in raw.items()}

    async def delete_group(self, group_id: str) -> bool:
        """删除用户组主记录（不含 members/lookup，由 GroupStore 统一清理）。"""
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        return bool(await self.redis.delete(f"aigateway:group:{group_id}"))

    async def set_group_lookup(self, name: str, group_id: str) -> None:
        """组名 -> group_id 反查。Key: aigateway:group_lookup:{name}"""
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        await self.redis.set(f"aigateway:group_lookup:{name}", group_id)

    async def get_group_lookup(self, name: str) -> str | None:
        """通过组名反查 group_id。"""
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        val = await self.redis.get(f"aigateway:group_lookup:{name}")
        return val.decode() if val else None

    async def delete_group_lookup(self, name: str) -> None:
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        await self.redis.delete(f"aigateway:group_lookup:{name}")

    # ------------------------------------------------------------------
    # 便捷操作 — 配额存储 (DB_SCHEMA §2)
    # ------------------------------------------------------------------

    async def set_quota(self, key_hash: str, period: str, data: dict) -> None:
        """写入配额累计数据。

        Key 格式: aigateway:quota:{key_hash}:{period}

        Args:
            key_hash: API Key hash。
            period: 周期标识，如 "daily:2024-01-21" 或 "monthly:2024-01"。
            data: 配额字段 {tokens_in, tokens_out, cost_usd, request_count, model_usage}。
        """
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        await self.redis.hset(
            f"aigateway:quota:{key_hash}:{period}", mapping=data
        )

    async def get_quota(self, key_hash: str, period: str) -> dict | None:
        """查询配额累计数据。

        Args:
            key_hash: API Key hash。
            period: 周期标识。

        Returns:
            配额字段字典，不存在返回 None。
        """
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        raw = await self.redis.hgetall(f"aigateway:quota:{key_hash}:{period}")
        if not raw:
            return None
        return {k.decode(): v.decode() for k, v in raw.items()}

    # ------------------------------------------------------------------
    # 便捷操作 — 速率限制 (DB_SCHEMA §5)
    # ------------------------------------------------------------------

    async def add_rpm_entry(self, key_hash: str, request_id: str, score: float) -> int:
        """向 RPM 有序集合中添加请求记录。

        Key 格式: aigateway:ratelimit:{key_hash}:rpm
        存储类型: Sorted Set，member=request_id, score=unix_timestamp
        """
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        pipe = self.redis.pipeline(transaction=False)
        pipe.zadd(
            f"aigateway:ratelimit:{key_hash}:rpm",
            {request_id: score}
        )
        pipe.expire(f"aigateway:ratelimit:{key_hash}:rpm", 120)
        results = await pipe.execute()
        return results[0]  # 新增条目数

    async def clean_old_rpm_entries(self, key_hash: str, before: float) -> int:
        """清理指定时间点之前的 RPM 记录。"""
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        return await self.redis.zremrangebyscore(
            f"aigateway:ratelimit:{key_hash}:rpm", "-inf", before
        )

    async def count_rpm_recent(self, key_hash: str, since: float) -> int:
        """统计当前窗口内的请求数。"""
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        return await self.redis.zcount(
            f"aigateway:ratelimit:{key_hash}:rpm", since, "+inf"
        )

    async def set_tpm_window(self, key_hash: str, tokens: int) -> None:
        """写入 TPM 窗口计数器。

        Key 格式: aigateway:ratelimit:{key_hash}:tpm
        存储类型: String，值为当前窗口累计 token 数
        """
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        await self.redis.set(
            f"aigateway:ratelimit:{key_hash}:tpm", str(tokens), ex=60
        )

    async def get_tpm_window(self, key_hash: str) -> int:
        """读取当前 TPM 窗口累计 token 数。"""
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        val = await self.redis.get(f"aigateway:ratelimit:{key_hash}:tpm")
        return int(val) if val else 0

    # ------------------------------------------------------------------
    # 原子管道 — 多步写操作的事务封装
    # ------------------------------------------------------------------

    async def pipe_batch(self, fn) -> list:
        """Run a sequence of Redis commands in an atomic pipeline.

        Usage::

            results = await redis.pipe_batch(lambda p: [
                p.hset("key:a", mapping={"x": "1"}),
                p.sadd("key:b", "member"),
                p.set("key:c", "val"),
            ])

        Internally creates a ``pipeline(transaction=True)`` so all commands
        execute atomically inside ``MULTI/EXEC``.  Raises ``redis.WatchError``
        on contention (caller should retry if needed).

        Returns:
            List of per-command results (same order as the list returned by
            ``fn``).
        """
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        pipe = self.redis.pipeline(transaction=True)
        commands = fn(pipe)
        # If the callable returned a list of commands, execute them;
        # otherwise (legacy single-call style) just execute the pipe.
        if isinstance(commands, list):
            return await pipe.execute()
        return await pipe.execute()


# 全局单例（懒初始化）
_redis_manager: RedisClientManager | None = None


def get_redis_manager() -> RedisClientManager:
    """获取全局 Redis 客户端管理器单例。"""
    global _redis_manager
    if _redis_manager is None:
        _redis_manager = RedisClientManager()
    return _redis_manager
