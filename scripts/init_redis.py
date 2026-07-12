"""
Redis 初始化脚本
===============

在首次部署时创建必要的 Redis 数据结构：
- 默认 API Key 存储结构（DB_SCHEMA §1）
- Pub/Sub 频道（DB_SCHEMA §4）
- 配额计数 Key 模板（DB_SCHEMA §2）
- 速率限制 Key 模板（DB_SCHEMA §5）

用法:
    python scripts/init_redis.py [--url redis://localhost:6379/0]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid

# 将项目根目录加入 PYTHONPATH（相对脚本位置解析，避免硬编码绝对路径）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "aigateway-core", "src"))

from aigateway_core.shared.redis_client import RedisClientManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("init_redis")


def _print_step(label: str) -> None:
    """打印步骤标题。"""
    print(f"\n{'=' * 60}")
    print(f"  [{label}]")
    print(f"{'=' * 60}")


async def init_default_api_keys(redis_mgr: RedisClientManager, api_keys: list[dict]) -> None:
    """创建默认的 API Key 记录。

    从环境变量 AI_GATEWAY_API_KEYS 读取预置 Key（逗号分隔）。
    """
    _print_step("创建默认 API Key")

    import os
    raw_keys = os.environ.get("AI_GATEWAY_API_KEYS", "")
    if raw_keys:
        keys_list = [k.strip() for k in raw_keys.split(",") if k.strip()]
    else:
        # 如果未配置环境变量，创建开发用默认 Key
        keys_list = [f"gw-dev-{uuid.uuid4().hex[:16]}"]

    for raw_key in keys_list:
        key_hash = _hash_key(raw_key)
        key_prefix = raw_key[:8]
        now_iso = _now_iso()

        key_data: dict[str, str] = {
            "key_id": f"key_{uuid.uuid4().hex[:8]}",
            "key_prefix": key_prefix,
            "user_id": "dev-default",
            "status": "active",
            "created_at": now_iso,
            "last_used_at": "",
            "daily_tokens_limit": "1000000",
            "daily_tokens_used": "0",
            "monthly_cost_limit": "50.0",
            "monthly_cost_used": "0.0",
            "rate_limit_rpm": "60",
            "rate_limit_tpm": "100000",
            "rpm_window_start": str(_now_unix()),
            "rpm_window_count": "0",
            "tpm_window_start": str(_now_unix()),
            "tpm_window_count": "0",
        }

        await redis_mgr.set_api_key(key_hash, key_data)
        await redis_mgr.set_key_lookup(key_prefix, key_hash)

        print(f"  已创建 API Key:")
        print(f"    key_prefix : {key_prefix}...")
        print(f"    key_id     : {key_data['key_id']}")
        print(f"    user_id    : {key_data['user_id']}")
        print(f"    status     : {key_data['status']}")

    if keys_list:
        print(f"\n  总计创建 {len(keys_list)} 个默认 API Key")


def _hash_key(key_value: str) -> str:
    """计算 SHA-256 哈希前 16 位。"""
    import hashlib
    return hashlib.sha256(key_value.encode()).hexdigest()[:16]


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_unix() -> int:
    from datetime import datetime, timezone
    return int(datetime.now(timezone.utc).timestamp())


async def setup_pubsub_channels(redis_mgr: RedisClientManager) -> None:
    """创建 Pub/Sub 频道注册（DB_SCHEMA §4）。

    注意：Redis Pub/Sub 频道是临时概念，无需预先创建。
    此处仅打印确认频道名，确保配置与 DB_SCHEMA 一致。
    """
    _print_step("Pub/Sub 频道配置")

    channels = [
        {
            "name": "aigateway:keys:sync",
            "description": "API Key 变更广播（创建/撤销/更新）",
            "message_format": 'JSON: {event_type, key_id, user_id, timestamp}',
        },
        {
            "name": "aigateway:config:reload",
            "description": "配置热加载通知",
            "message_format": 'JSON: {event_type, config_version, timestamp}',
        },
    ]

    for ch in channels:
        print(f"  频道: {ch['name']}")
        print(f"    用途   : {ch['description']}")
        print(f"    消息格式: {ch['message_format']}")

    print("\n  注：Redis Pub/Sub 频道为按需创建，无需预先注册")


async def setup_rate_limit_templates(redis_mgr: RedisClientManager) -> None:
    """创建速率限制 Key 模板（DB_SCHEMA §5）。

    说明：实际的速率限制 Key 在首次请求时动态创建，
    此处仅打印模板格式说明。
    """
    _print_step("速率限制 Key 模板")

    templates = [
        {
            "format": "aigateway:ratelimit:{key_hash}:rpm",
            "type": "Sorted Set",
            "ttl": "120 秒",
            "members": "request_id (UUID) -> score (unix_timestamp)",
        },
        {
            "format": "aigateway:ratelimit:{key_hash}:tpm",
            "type": "String",
            "ttl": "60 秒",
            "value": "当前窗口内累计 token 数",
        },
    ]

    for t in templates:
        print(f"  模板: {t['format']}")
        print(f"    存储类型: {t['type']}")
        print(f"    TTL     : {t['ttl']}")
        for k, v in t.items():
            if k not in ("format", "type", "ttl"):
                print(f"    {k}: {v}")


async def setup_quota_templates(redis_mgr: RedisClientManager) -> None:
    """打印配额计数 Key 模板说明（DB_SCHEMA §2）。"""
    _print_step("配额计数 Key 模板")

    templates = [
        {
            "format": "aigateway:quota:{key_hash}:daily:{YYYY-MM-DD}",
            "type": "Hash",
            "ttl": "当日 23:59:59 UTC 自动过期",
            "fields": "tokens_in, tokens_out, cost_usd, request_count, model_usage",
        },
        {
            "format": "aigateway:quota:{key_hash}:monthly:{YYYY-MM}",
            "type": "Hash",
            "ttl": "当月最后一天 23:59:59 UTC 自动过期",
            "fields": "tokens_in, tokens_out, cost_usd, request_count, model_usage",
        },
        {
            "format": "aigateway:alert:{key_hash}:{type}",
            "type": "String",
            "ttl": "300 秒",
            "values": '"triggered" | "acknowledged"',
        },
    ]

    for t in templates:
        print(f"  模板: {t['format']}")
        print(f"    存储类型: {t['type']}")
        print(f"    TTL     : {t['ttl']}")
        print(f"    字段    : {t.get('fields', '')}")
        print(f"    值     : {t.get('values', '')}")


async def main() -> None:
    """主入口。"""
    parser = argparse.ArgumentParser(description="初始化 Redis 数据结构")
    parser.add_argument(
        "--url",
        default="redis://localhost:6379/0",
        help="Redis 连接地址 (默认: redis://localhost:6379/0)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  AI Gateway Redis 初始化")
    print(f"  连接地址: {args.url}")
    print("=" * 60)

    redis_mgr = RedisClientManager()

    try:
        # 1. 连接 Redis
        _print_step("连接 Redis")
        await redis_mgr.connect(args.url)
        print("  Redis 连接成功")

        # 2. 初始化默认 API Key
        await init_default_api_keys(redis_mgr, [])

        # 3. 配置 Pub/Sub 频道
        await setup_pubsub_channels(redis_mgr)

        # 4. 速率限制 Key 模板
        await setup_rate_limit_templates(redis_mgr)

        # 5. 配额计数 Key 模板
        await setup_quota_templates(redis_mgr)

        print(f"\n{'=' * 60}")
        print("  初始化完成！")
        print(f"{'=' * 60}\n")

    except ConnectionError as exc:
        print(f"\n  [错误] Redis 连接失败: {exc}")
        print("  请确保 Redis 服务正在运行")
        sys.exit(1)
    except Exception as exc:
        print(f"\n  [错误] 初始化失败: {exc}")
        logger.exception("初始化异常")
        sys.exit(1)
    finally:
        await redis_mgr.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
