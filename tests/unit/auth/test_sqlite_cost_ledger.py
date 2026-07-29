from __future__ import annotations

import sqlite3

import pytest
from aigateway_core.shared.auth.sqlite_store import SQLiteStore


@pytest.mark.asyncio
async def test_cost_ledger_migrates_and_aggregates_durable_overview_metrics(
    tmp_path,
):
    db_path = tmp_path / "auth.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE request_cost_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT DEFAULT '',
            ts TEXT NOT NULL,
            ts_unix INTEGER NOT NULL,
            user_id TEXT DEFAULT '',
            group_id TEXT DEFAULT '',
            model TEXT DEFAULT '',
            provider TEXT DEFAULT '',
            pipeline_kind TEXT DEFAULT '',
            tokens_in INTEGER DEFAULT 0,
            tokens_out INTEGER DEFAULT 0,
            tokens_total INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            cached INTEGER DEFAULT 0,
            stream INTEGER DEFAULT 0,
            status TEXT DEFAULT 'ok'
        )
        """
    )
    connection.commit()
    connection.close()

    store = SQLiteStore(str(db_path))
    columns = {
        row["name"]
        for row in store.conn.fetchall("PRAGMA table_info(request_cost_ledger)")
    }
    assert {"tokens_saved", "duration_ms"} <= columns

    await store.record_request_cost(
        trace_id="trace-provider",
        user_id="alice",
        model="gpt",
        tokens_in=80,
        tokens_out=20,
        tokens_total=100,
        cost_usd=1.25,
        duration_ms=200,
    )
    await store.record_request_cost(
        trace_id="trace-cache",
        user_id="alice",
        model="gpt",
        tokens_saved=100,
        cached=True,
        duration_ms=20,
    )

    summary = await store.ledger_summary()

    assert summary["total"]["requests"] == 2
    assert summary["total"]["cost_usd"] == pytest.approx(1.25)
    assert summary["total"]["cache_hits"] == 1
    assert summary["total"]["tokens_saved"] == 100
    assert summary["total"]["avg_latency_ms"] == pytest.approx(110)
    assert summary["latency_by_hour"][-1]["samples"] == 2
    assert summary["latency_by_hour"][-1]["avg_latency_ms"] == pytest.approx(110)

    store.close()
