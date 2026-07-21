"""Tests for SQLiteStore quota race conditions.

Verifies the atomic check_quota implementation prevents concurrent requests
from exceeding quotas.
"""

import asyncio
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.shared.auth.sqlite_store import SQLiteStore, _hash_key


@pytest.mark.asyncio
async def test_concurrent_quota_check_prevents_overuse():
    """Multiple concurrent quota checks should not allow over-quota usage."""
    db_path = f"/tmp/test_quota_race_{os.getpid()}.db"
    try:
        store = SQLiteStore(db_path=db_path)

        # Create API key with low daily token limit
        result = await store.create(
            user_id="test-user",
            quotas={"daily_tokens": 100},
            group_id="",
            cache_scope="group"
        )
        kh = _hash_key(result["key"])

        # Simulate 10 concurrent requests trying to use 20 tokens each
        # Total requested: 200 tokens (exceeds 100 limit)
        # Should only allow ~5 requests through

        async def check_quota_task(i):
            allowed = await store.check_quota(
                key_hash=kh,
                tokens=20,
                cost=0.0,
            )
            return allowed

        results = await asyncio.gather(*[check_quota_task(i) for i in range(10)])
        allowed_count = sum(1 for r in results if r[0])

        # Should not exceed quota
        assert allowed_count <= 5  # 100 tokens / 20 per request = 5 max
        assert allowed_count >= 1  # At least some should succeed

    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


@pytest.mark.asyncio
async def test_concurrent_rpm_check_prevents_overuse():
    """Multiple concurrent requests should respect RPM limit."""
    db_path = f"/tmp/test_rpm_race_{os.getpid()}.db"
    try:
        store = SQLiteStore(db_path=db_path)

        result = await store.create(
            user_id="test-user-rpm",
            quotas={"rate_limit_rpm": 2},
            group_id="",
            cache_scope="group"
        )
        kh = _hash_key(result["key"])

        async def check_quota_task(i):
            return await store.check_quota(
                key_hash=kh,
                tokens=10,
                cost=0.01,
            )

        results = await asyncio.gather(*[check_quota_task(i) for i in range(10)])
        allowed_count = sum(1 for r in results if r[0])

        # Should not exceed RPM limit
        assert allowed_count <= 2

    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


@pytest.mark.asyncio
async def test_concurrent_monthly_cost_prevents_overuse():
    """Concurrent requests should respect monthly cost limit."""
    db_path = f"/tmp/test_monthly_race_{os.getpid()}.db"
    try:
        store = SQLiteStore(db_path=db_path)

        result = await store.create(
            user_id="test-user-monthly",
            quotas={"monthly_cost": 1.0},
            group_id="",
            cache_scope="group"
        )
        kh = _hash_key(result["key"])

        async def check_quota_task(i):
            return await store.check_quota(
                key_hash=kh,
                tokens=10,
                cost=0.3,
            )

        results = await asyncio.gather(*[check_quota_task(i) for i in range(10)])
        allowed_count = sum(1 for r in results if r[0])

        # Should not exceed monthly cost limit (1.0 / 0.3 = 3 max)
        assert allowed_count <= 3
        assert allowed_count >= 1

    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


@pytest.mark.asyncio
async def test_sequential_quota_exhaustion():
    """Sequential quota checks should exhaust quota correctly."""
    db_path = f"/tmp/test_sequential_quota_{os.getpid()}.db"
    try:
        store = SQLiteStore(db_path=db_path)

        result = await store.create(
            user_id="test-user-seq",
            quotas={"daily_tokens": 50},
            group_id="",
            cache_scope="group"
        )
        kh = _hash_key(result["key"])

        # First 2 requests should pass (50 tokens / 20 each = 2)
        ok1, _, _ = await store.check_quota(key_hash=kh, tokens=20, cost=0.0)
        ok2, _, _ = await store.check_quota(key_hash=kh, tokens=20, cost=0.0)
        assert ok1 is True
        assert ok2 is True

        # Third request should fail (40 + 20 > 50)
        ok3, reason, _ = await store.check_quota(key_hash=kh, tokens=20, cost=0.0)
        assert ok3 is False
        assert "Daily" in reason

    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


@pytest.mark.asyncio
async def test_check_quota_unknown_key_returns_false():
    """Quota check for non-existent key should return False."""
    db_path = f"/tmp/test_unknown_key_{os.getpid()}.db"
    try:
        store = SQLiteStore(db_path=db_path)

        ok, reason, retry = await store.check_quota(
            key_hash="nonexistent_hash_1234567890abcdef",
            tokens=10,
            cost=0.0,
        )

        assert ok is False
        assert "does not exist" in reason

    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


@pytest.mark.asyncio
async def test_group_quota_concurrent_respected():
    """Concurrent requests with shared group quota should not exceed limits."""
    db_path = f"/tmp/test_group_race_{os.getpid()}.db"
    try:
        store = SQLiteStore(db_path=db_path)

        # Create group first
        group = await store.create_group("TestGroup", {"daily_tokens": 100})
        gid = group["group_id"]

        # Create two keys in the same group
        result1 = await store.create(
            user_id="user1",
            quotas={"daily_tokens": 1000},  # high personal limit
            group_id=gid,
            cache_scope="group"
        )
        result2 = await store.create(
            user_id="user2",
            quotas={"daily_tokens": 1000},  # high personal limit
            group_id=gid,
            cache_scope="group"
        )

        kh1 = _hash_key(result1["key"])
        kh2 = _hash_key(result2["key"])

        async def check_quota_task(key_hash, i):
            return await store.check_quota(
                key_hash=key_hash,
                tokens=30,
                cost=0.0,
            )

        # Mix requests from both keys concurrently
        tasks = [
            check_quota_task(kh1, 0),
            check_quota_task(kh2, 1),
            check_quota_task(kh1, 2),
            check_quota_task(kh2, 3),
        ]
        results = await asyncio.gather(*tasks)
        allowed_count = sum(1 for r in results if r[0])

        # Group daily limit is 100, each request uses 30 -> max 3 allowed
        assert allowed_count <= 3
        assert allowed_count >= 1

    finally:
        if os.path.exists(db_path):
            os.remove(db_path)
