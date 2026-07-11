"""Real-Redis integration tests for KeyStore.check_quota Lua script.

These tests require a live Redis instance (the Lua ``eval``/``evalsha`` path
that ``check_quota`` uses in production). FakeRedis has no ``eval`` support,
so ``check_quota`` falls back to the non-atomic legacy path there — meaning
the production Lua script had ZERO test coverage before this file.

Skip entirely if Redis is unavailable (CI without redis).
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from aigateway_core.shared.auth.key_store import KeyStore


def _redis_available() -> bool:
    try:
        import redis  # noqa: F401
    except ImportError:
        return False
    try:
        import redis
        url = os.environ.get("AI_GATEWAY_REDIS_URL", "redis://localhost:6379/0")
        r = redis.from_url(url, socket_connect_timeout=1)
        r.ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _redis_available(),
    reason="Live Redis unavailable — Lua quota path needs real eval support",
)


def _key_data(**overrides):
    base = {
        "key_id": "k1", "user_id": "u1", "status": "active",
        "group_id": "", "cache_scope": "group",
        "daily_tokens_limit": "1000000", "daily_tokens_used": "0",
        "monthly_cost_limit": "50.0", "monthly_cost_used": "0.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0",
    }
    base.update(overrides)
    return base


@pytest_asyncio.fixture
async def ks_real():
    """Real Redis-backed KeyStore with a clean namespace per test."""
    from aigateway_core.shared.redis_client import RedisClientManager

    async def _purge(redis):
        """Delete every aigateway:* key (loop SCAN to completion)."""
        cursor = 0
        while True:
            cursor, keys = await redis.scan(cursor, match="aigateway:*", count=1000)
            if keys:
                await redis.delete(*keys)
            if cursor == 0:
                break

    url = os.environ.get("AI_GATEWAY_REDIS_URL", "redis://localhost:6379/0")
    mgr = RedisClientManager()
    await mgr.connect(url=url)
    await _purge(mgr.redis)

    ks = KeyStore(redis=mgr)
    yield ks

    # Teardown
    await _purge(mgr.redis)
    try:
        await mgr.disconnect()
    except Exception:
        pass


@pytest.mark.asyncio
async def test_lua_check_quota_passes_under_limits(ks_real):
    """All dims under limit → pass, counters bumped atomically."""
    ks = ks_real
    await ks.redis.set_api_key("kh1", _key_data(daily_tokens_limit="1000"))
    ok, reason, retry = await ks.check_quota("kh1", tokens=100, cost=0.0)
    assert ok is True, f"expected pass, got {reason}"
    assert reason is None
    data = await ks.redis.get_api_key("kh1")
    # Lua bumped daily_tokens_used by 100, RPM by 1, TPM by 100
    assert data["daily_tokens_used"] == "100"
    assert int(data["rpm_window_count"]) == 1
    assert int(data["tpm_window_count"]) == 100


@pytest.mark.asyncio
async def test_lua_check_quota_rejects_daily_limit(ks_real):
    """Daily token limit exceeded → reject with 'Daily' reason, no bump."""
    ks = ks_real
    await ks.redis.set_api_key("kh1", _key_data(
        daily_tokens_limit="1000", daily_tokens_used="950"))
    ok, reason, retry = await ks.check_quota("kh1", tokens=100, cost=0.0)
    assert ok is False
    assert "Daily" in reason
    # Failed check must NOT bump counters
    data = await ks.redis.get_api_key("kh1")
    assert data["daily_tokens_used"] == "950"


@pytest.mark.asyncio
async def test_lua_check_quota_rejects_rpm(ks_real):
    """RPM limit exceeded → reject with 'RPM' reason (the index-shift bug
    previously made every request fail TPM before RPM was ever checked)."""
    ks = ks_real
    await ks.redis.set_api_key("kh1", _key_data(rate_limit_rpm="2",
        rpm_window_count="2", rpm_window_start=str(int(datetime.now(timezone.utc).timestamp()))))
    ok, reason, retry = await ks.check_quota("kh1", tokens=10, cost=0.0)
    assert ok is False
    assert "RPM" in reason
    assert retry > 0  # retry-after should be the remaining window


@pytest.mark.asyncio
async def test_lua_check_quota_rejects_tpm(ks_real):
    """TPM limit exceeded → reject with 'TPM' reason."""
    ks = ks_real
    await ks.redis.set_api_key("kh1", _key_data(rate_limit_tpm="100",
        tpm_window_count="95", tpm_window_start=str(int(datetime.now(timezone.utc).timestamp()))))
    ok, reason, retry = await ks.check_quota("kh1", tokens=10, cost=0.0)
    assert ok is False
    assert "TPM" in reason


@pytest.mark.asyncio
async def test_lua_check_quota_rejects_monthly_cost(ks_real):
    """Monthly cost limit exceeded → reject with 'Monthly' reason."""
    ks = ks_real
    await ks.redis.set_api_key("kh1", _key_data(
        monthly_cost_limit="50.0", monthly_cost_used="49.0"))
    ok, reason, retry = await ks.check_quota("kh1", tokens=10, cost=2.0)
    assert ok is False
    assert "Monthly" in reason


@pytest.mark.asyncio
async def test_lua_check_quota_group_level_reject(ks_real):
    """Group daily limit exceeded → reject with 'Group ' prefix."""
    ks = ks_real
    await ks.redis.set_group("grp-g", _key_data(
        daily_tokens_limit="100", daily_tokens_used="95",
        monthly_cost_limit="5000"))
    await ks.redis.set_api_key("kh1", _key_data(
        group_id="grp-g", daily_tokens_limit="1000000"))
    ok, reason, retry = await ks.check_quota("kh1", tokens=10, cost=0.0)
    assert ok is False
    assert reason.startswith("Group ")
    assert "daily" in reason.lower()


@pytest.mark.asyncio
async def test_lua_increments_group_counters(ks_real):
    """Successful check on a grouped key bumps BOTH key and group counters."""
    ks = ks_real
    await ks.redis.set_group("grp-g", _key_data(
        daily_tokens_limit="10000", monthly_cost_limit="5000"))
    await ks.redis.set_api_key("kh1", _key_data(
        group_id="grp-g", daily_tokens_limit="10000"))
    ok, reason, retry = await ks.check_quota("kh1", tokens=100, cost=1.5)
    assert ok is True, reason
    kdata = await ks.redis.get_api_key("kh1")
    gdata = await ks.redis.get_group("grp-g")
    assert kdata["daily_tokens_used"] == "100"
    assert gdata["daily_tokens_used"] == "100"
    assert float(kdata["monthly_cost_used"]) == 1.5
    assert float(gdata["monthly_cost_used"]) == 1.5


@pytest.mark.asyncio
async def test_lua_writes_quota_period_records(ks_real):
    """Lua script also writes the daily/monthly quota period hashes."""
    ks = ks_real
    await ks.redis.set_api_key("kh1", _key_data(daily_tokens_limit="10000"))
    await ks.check_quota("kh1", tokens=100, cost=2.0)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    daily = await ks.redis.get_quota("kh1", f"daily:{today}")
    monthly = await ks.redis.get_quota("kh1", f"monthly:{month}")
    assert int(daily.get("tokens_in", 0)) == 100
    assert int(daily.get("tokens_out", 0)) == 100
    assert float(monthly.get("cost_usd", 0)) == 2.0


@pytest.mark.asyncio
async def test_lua_atomicity_concurrent(ks_real):
    """Two concurrent check_quota calls against a tight daily limit: the
    sum that exceeds the limit must be rejected (atomic reserve)."""
    ks = ks_real
    # Limit 100 tokens; each request asks for 80. Two concurrent → one must fail.
    await ks.redis.set_api_key("kh1", _key_data(daily_tokens_limit="100"))
    results = await asyncio.gather(
        ks.check_quota("kh1", tokens=80, cost=0.0),
        ks.check_quota("kh1", tokens=80, cost=0.0),
    )
    oks = [r[0] for r in results]
    # Exactly one passes (80 ≤ 100), the other rejected (160 > 100)
    assert oks.count(True) == 1, f"expected 1 pass, got {oks}"
    assert oks.count(False) == 1
    data = await ks.redis.get_api_key("kh1")
    # The single passing reserve bumped daily by 80
    assert data["daily_tokens_used"] == "80"
