"""Period rollover and multi-connection regression tests for SQLite quotas."""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from aigateway_core.shared.auth.sqlite_store import SQLiteStore, _hash_key


@pytest.mark.asyncio
async def test_quota_records_accumulate_instead_of_replacing(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "quota.db"))
    created = await store.create(user_id="accumulate-user")
    key_hash = _hash_key(created["key"])

    await store.increment_usage(
        key_hash,
        tokens=30,
        cost=0.03,
        model="model-a",
        tokens_in=10,
        tokens_out=20,
    )
    await store.increment_usage(
        key_hash,
        tokens=70,
        cost=0.07,
        model="model-a",
        tokens_in=30,
        tokens_out=40,
    )

    now = store._quota_now()
    rows = store._quota_period_rows(
        "key",
        key_hash,
        now.strftime("%Y-%m-%d"),
        now.strftime("%Y-%m"),
    )
    for period in ("daily", "monthly"):
        row = rows[period]
        assert int(row["tokens_in"]) == 40
        assert int(row["tokens_out"]) == 60
        assert float(row["cost_usd"]) == pytest.approx(0.10)
        assert int(row["request_count"]) == 2
        usage = json.loads(row["model_usage"])
        assert usage["model-a"]["in"] == 40
        assert usage["model-a"]["out"] == 60


@pytest.mark.asyncio
async def test_daily_and_monthly_counters_roll_over_in_utc(
    tmp_path,
    monkeypatch,
) -> None:
    clock = {"now": datetime(2026, 8, 4, 23, 59, tzinfo=UTC)}
    monkeypatch.setattr(SQLiteStore, "_quota_now", lambda self: clock["now"])
    store = SQLiteStore(str(tmp_path / "period.db"))
    created = await store.create(
        user_id="period-user",
        quotas={
            "daily_tokens": 100,
            "monthly_cost": 1.0,
            "rate_limit_rpm": 100,
            "rate_limit_tpm": 10000,
        },
    )
    key_hash = _hash_key(created["key"])

    allowed, reason, _ = await store.check_quota(key_hash, 80, 0.8)
    assert allowed is True, reason

    clock["now"] = datetime(2026, 8, 5, 0, 1, tzinfo=UTC)
    allowed, reason, _ = await store.check_quota(key_hash, 30, 0.1)
    assert allowed is True, reason
    row = dict(store.conn.fetchone("SELECT * FROM api_keys WHERE key_hash=?", (key_hash,)))
    assert row["daily_period"] == "2026-08-05"
    assert int(row["daily_tokens_used"]) == 30
    assert row["monthly_period"] == "2026-08"
    assert float(row["monthly_cost_used"]) == pytest.approx(0.9)

    clock["now"] = datetime(2026, 9, 1, 0, 1, tzinfo=UTC)
    allowed, reason, _ = await store.check_quota(key_hash, 10, 0.5)
    assert allowed is True, reason
    row = dict(store.conn.fetchone("SELECT * FROM api_keys WHERE key_hash=?", (key_hash,)))
    assert row["daily_period"] == "2026-09-01"
    assert int(row["daily_tokens_used"]) == 10
    assert row["monthly_period"] == "2026-09"
    assert float(row["monthly_cost_used"]) == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_group_tpm_is_reconciled_to_actual_usage(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "group-reconcile.db"))
    group = await store.create_group(
        "reconcile-group",
        {
            "daily_tokens": 1000,
            "monthly_cost": 100,
            "rate_limit_rpm": 100,
            "rate_limit_tpm": 1000,
        },
    )
    created = await store.create(
        user_id="group-reconcile-user",
        group_id=group["group_id"],
        quotas={
            "daily_tokens": 1000,
            "monthly_cost": 100,
            "rate_limit_rpm": 100,
            "rate_limit_tpm": 1000,
        },
    )
    key_hash = _hash_key(created["key"])

    allowed, reason, _ = await store.check_quota(key_hash, 100, 1.0)
    assert allowed is True, reason
    await store.increment_usage(
        key_hash,
        tokens=20,
        cost=0.2,
        model="model-a",
        tokens_in=8,
        tokens_out=12,
        _lua_already_incr=True,
        _reserved_tokens=100,
        _reserved_cost=1.0,
    )

    key = dict(store.conn.fetchone("SELECT * FROM api_keys WHERE key_hash=?", (key_hash,)))
    group_row = dict(
        store.conn.fetchone(
            "SELECT * FROM groups WHERE group_id=?",
            (group["group_id"],),
        )
    )
    assert int(key["tpm_window_count"]) == 20
    assert int(group_row["tpm_window_count"]) == 20
    assert int(key["daily_tokens_used"]) == 20
    assert int(group_row["daily_tokens_used"]) == 20


@pytest.mark.asyncio
async def test_release_reservation_releases_group_tpm(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "group-release.db"))
    group = await store.create_group(
        "release-group",
        {"rate_limit_rpm": 100, "rate_limit_tpm": 1000},
    )
    created = await store.create(
        user_id="group-release-user",
        group_id=group["group_id"],
        quotas={"rate_limit_rpm": 100, "rate_limit_tpm": 1000},
    )
    key_hash = _hash_key(created["key"])

    allowed, reason, _ = await store.check_quota(key_hash, 50, 0.5)
    assert allowed is True, reason
    await store.release_reserved_usage(
        key_hash,
        reserved_tokens=50,
        reserved_cost=0.5,
    )

    key = dict(store.conn.fetchone("SELECT * FROM api_keys WHERE key_hash=?", (key_hash,)))
    group_row = dict(
        store.conn.fetchone(
            "SELECT * FROM groups WHERE group_id=?",
            (group["group_id"],),
        )
    )
    assert int(key["tpm_window_count"]) == 0
    assert int(group_row["tpm_window_count"]) == 0


@pytest.mark.asyncio
async def test_sub_cent_monthly_reservations_are_not_rounded_to_zero(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "precision.db"))
    created = await store.create(
        user_id="precision-user",
        quotas={
            "daily_tokens": 1000,
            "monthly_cost": 0.00005,
            "rate_limit_rpm": 100,
            "rate_limit_tpm": 10000,
        },
    )
    key_hash = _hash_key(created["key"])

    first, reason, _ = await store.check_quota(key_hash, 1, 0.00003)
    assert first is True, reason
    second, reason, _ = await store.check_quota(key_hash, 1, 0.00003)
    assert second is False
    assert "monthly cost" in str(reason).lower()


def test_independent_connections_cannot_overbook_daily_quota(tmp_path) -> None:
    db_path = str(tmp_path / "multi-connection.db")
    seed = SQLiteStore(db_path)
    created = asyncio.run(
        seed.create(
            user_id="multi-connection-user",
            quotas={
                "daily_tokens": 100,
                "monthly_cost": 100,
                "rate_limit_rpm": 100,
                "rate_limit_tpm": 10000,
            },
        )
    )
    key_hash = _hash_key(created["key"])

    def reserve() -> bool:
        store = SQLiteStore(db_path)
        try:
            return asyncio.run(store.check_quota(key_hash, 20, 0.0))[0]
        finally:
            store.conn.close()

    with ThreadPoolExecutor(max_workers=10) as executor:
        allowed = list(executor.map(lambda _: reserve(), range(10)))

    assert sum(allowed) == 5
    row = dict(seed.conn.fetchone("SELECT * FROM api_keys WHERE key_hash=?", (key_hash,)))
    assert int(row["daily_tokens_used"]) == 100


def test_independent_connections_do_not_lose_usage_records(tmp_path) -> None:
    db_path = str(tmp_path / "multi-record.db")
    seed = SQLiteStore(db_path)
    created = asyncio.run(seed.create(user_id="multi-record-user"))
    key_hash = _hash_key(created["key"])

    def record() -> None:
        store = SQLiteStore(db_path)
        try:
            asyncio.run(
                store.increment_usage(
                    key_hash,
                    tokens=10,
                    cost=0.01,
                    model="model-a",
                    tokens_in=4,
                    tokens_out=6,
                )
            )
        finally:
            store.conn.close()

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: record(), range(8)))

    now = seed._quota_now()
    rows = seed._quota_period_rows(
        "key",
        key_hash,
        now.strftime("%Y-%m-%d"),
        now.strftime("%Y-%m"),
    )
    daily = rows["daily"]
    assert int(daily["tokens_in"]) == 32
    assert int(daily["tokens_out"]) == 48
    assert int(daily["request_count"]) == 8
    assert float(daily["cost_usd"]) == pytest.approx(0.08)
