"""Public SQLite auth store with deterministic config-backed path resolution."""
from __future__ import annotations

import json
import math
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from aigateway_core.shared.runtime_values import (
    configured_model_pricing,
    configured_path,
)

from . import _sqlite_store_impl as _impl


class SQLiteStore(_impl.SQLiteStore):
    """SQLite store with atomic, period-aware quota accounting.

    The implementation module retains the broad compatibility surface.  This
    public class owns path resolution and the quota transaction contract used by
    the running Gateway.  Quota writes use ``BEGIN IMMEDIATE`` so independent
    SQLite connections and future multi-worker deployments cannot overwrite one
    another's counters after reading the same snapshot.
    """

    _COUNTER_TABLES = {
        ("api_keys", "key_hash"),
        ("groups", "group_id"),
    }

    def __init__(self, db_path: str | None = None):
        selected = db_path or os.environ.get("AI_GATEWAY_AUTH_DB_PATH", "").strip()
        if selected:
            path = Path(selected).expanduser()
            if not path.is_absolute():
                config_file = Path(
                    os.environ.get("AI_GATEWAY_CONFIG_PATH", "./config.yaml")
                ).expanduser().resolve()
                path = config_file.parent / path
            resolved = str(path.resolve())
        else:
            resolved = configured_path("auth.database_path")
        super().__init__(db_path=resolved)
        self._ensure_quota_period_schema()

    def _quota_now(self) -> datetime:
        """Return the UTC clock used for quota periods; isolated for tests."""
        return datetime.now(UTC)

    @contextmanager
    def _immediate_transaction(self) -> Iterator[Any]:
        """Serialize a read/validate/write quota transaction across connections."""
        connection = self.conn._connect()
        if connection.in_transaction:
            raise RuntimeError("nested SQLite quota transaction is not supported")
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    def _ensure_quota_period_schema(self) -> None:
        """Add explicit UTC day/month ownership to legacy aggregate counters."""
        now = self._quota_now()
        today = now.strftime("%Y-%m-%d")
        month = now.strftime("%Y-%m")
        with self._immediate_transaction() as tx:
            for table in ("api_keys", "groups"):
                columns = {
                    str(row["name"])
                    for row in tx.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if "daily_period" not in columns:
                    tx.execute(
                        f"ALTER TABLE {table} ADD COLUMN daily_period TEXT DEFAULT ''"
                    )
                if "monthly_period" not in columns:
                    tx.execute(
                        f"ALTER TABLE {table} ADD COLUMN monthly_period TEXT DEFAULT ''"
                    )
                # Existing counters cannot be attributed retroactively. Preserve
                # them in the migration's current UTC period, then roll normally.
                tx.execute(
                    f"UPDATE {table} SET daily_period=? "
                    "WHERE daily_period IS NULL OR TRIM(daily_period)=''",
                    (today,),
                )
                tx.execute(
                    f"UPDATE {table} SET monthly_period=? "
                    "WHERE monthly_period IS NULL OR TRIM(monthly_period)=''",
                    (month,),
                )

    @staticmethod
    def _validated_usage(tokens: int, cost: float) -> tuple[int, float]:
        try:
            parsed_tokens = int(tokens)
            parsed_cost = float(cost)
        except (TypeError, ValueError) as exc:
            raise ValueError("quota usage must be numeric") from exc
        if parsed_tokens < 0:
            raise ValueError("quota tokens must be non-negative")
        if not math.isfinite(parsed_cost) or parsed_cost < 0:
            raise ValueError("quota cost must be a finite non-negative number")
        return parsed_tokens, parsed_cost

    @classmethod
    def _update_counter_row(
        cls,
        tx: Any,
        table: str,
        id_column: str,
        id_value: str,
        updates: dict[str, Any],
    ) -> None:
        if (table, id_column) not in cls._COUNTER_TABLES:
            raise ValueError("unsupported quota counter target")
        allowed = {
            "daily_period",
            "monthly_period",
            "daily_tokens_used",
            "monthly_cost_used",
            "rpm_window_start",
            "rpm_window_count",
            "tpm_window_start",
            "tpm_window_count",
        }
        fields = [name for name in updates if name in allowed]
        if not fields:
            return
        assignments = ", ".join(f"{name}=?" for name in fields)
        values = [updates[name] for name in fields]
        values.append(id_value)
        tx.execute(
            f"UPDATE {table} SET {assignments} WHERE {id_column}=?",
            tuple(values),
        )

    @classmethod
    def _normalize_periods(
        cls,
        tx: Any,
        table: str,
        id_column: str,
        id_value: str,
        row: Any,
        today: str,
        month: str,
    ) -> tuple[dict[str, Any], bool, bool]:
        data = dict(row)
        updates: dict[str, Any] = {}
        daily_reset = str(data.get("daily_period") or "") != today
        monthly_reset = str(data.get("monthly_period") or "") != month
        if daily_reset:
            updates.update(daily_period=today, daily_tokens_used=0)
        if monthly_reset:
            updates.update(monthly_period=month, monthly_cost_used=0.0)
        if updates:
            cls._update_counter_row(
                tx,
                table,
                id_column,
                id_value,
                updates,
            )
            data.update(updates)
        return data, daily_reset, monthly_reset

    @classmethod
    def _reservation_update(
        cls,
        data: dict[str, Any],
        tokens: int,
        cost: float,
        now_unix: int,
        *,
        group: bool,
    ) -> tuple[dict[str, Any] | None, str | None, int]:
        rpm_limit = int(data.get("rate_limit_rpm", cls.DEFAULT_RATE_LIMIT_RPM))
        rpm_start = int(data.get("rpm_window_start", 0))
        rpm_count = int(data.get("rpm_window_count", 0))
        if now_unix - rpm_start >= 60:
            new_rpm_start = now_unix
            new_rpm_count = 1
        else:
            new_rpm_start = rpm_start
            new_rpm_count = rpm_count + 1

        tpm_limit = int(data.get("rate_limit_tpm", cls.DEFAULT_RATE_LIMIT_TPM))
        tpm_start = int(data.get("tpm_window_start", 0))
        tpm_count = int(data.get("tpm_window_count", 0))
        if now_unix - tpm_start >= 60:
            new_tpm_start = now_unix
            new_tpm_count = tokens
        else:
            new_tpm_start = tpm_start
            new_tpm_count = tpm_count + tokens

        daily_limit = int(
            data.get("daily_tokens_limit", cls.DEFAULT_DAILY_TOKENS)
        )
        monthly_limit = float(
            data.get("monthly_cost_limit", cls.DEFAULT_MONTHLY_COST)
        )
        new_daily = int(data.get("daily_tokens_used", 0)) + tokens
        # Four-decimal rounding discarded legitimate sub-cent usage. Keep enough
        # precision for per-token prices while bounding floating point noise.
        new_monthly = round(float(data.get("monthly_cost_used", 0.0)) + cost, 12)
        prefix = "Group " if group else ""

        if new_rpm_count > rpm_limit:
            return (
                None,
                f"{prefix}RPM limit exceeded: {new_rpm_count}/{rpm_limit}",
                max(0, new_rpm_start + 60 - now_unix),
            )
        if new_tpm_count > tpm_limit:
            return (
                None,
                f"{prefix}TPM limit exceeded: {new_tpm_count}/{tpm_limit}",
                max(0, new_tpm_start + 60 - now_unix),
            )
        if new_daily > daily_limit:
            return (
                None,
                f"{prefix}daily token limit exceeded: "
                f"{int(data.get('daily_tokens_used', 0))}/{daily_limit}",
                0,
            )
        if new_monthly > monthly_limit:
            return (
                None,
                f"{prefix}monthly cost limit exceeded: "
                f"${float(data.get('monthly_cost_used', 0.0)):.6f}/"
                f"${monthly_limit:.6f}",
                0,
            )
        return (
            {
                "rpm_window_start": new_rpm_start,
                "rpm_window_count": new_rpm_count,
                "tpm_window_start": new_tpm_start,
                "tpm_window_count": new_tpm_count,
                "daily_tokens_used": new_daily,
                "monthly_cost_used": new_monthly,
            },
            None,
            0,
        )

    async def check_quota(
        self,
        key_hash: str,
        tokens: int,
        cost: float,
    ) -> tuple[bool, str | None, int]:
        """Atomically validate and reserve key and group quota dimensions."""
        tokens, cost = self._validated_usage(tokens, cost)
        now = self._quota_now()
        now_unix = int(now.timestamp())
        today = now.strftime("%Y-%m-%d")
        month = now.strftime("%Y-%m")

        with self._immediate_transaction() as tx:
            key_row = tx.execute(
                "SELECT * FROM api_keys WHERE key_hash=?",
                (key_hash,),
            ).fetchone()
            if key_row is None:
                return False, "API Key does not exist", 0
            key_data, _, _ = self._normalize_periods(
                tx,
                "api_keys",
                "key_hash",
                key_hash,
                key_row,
                today,
                month,
            )
            key_update, reason, retry = self._reservation_update(
                key_data,
                tokens,
                cost,
                now_unix,
                group=False,
            )
            if key_update is None:
                return False, reason, retry

            group_id = str(key_data.get("group_id") or "")
            group_update: dict[str, Any] | None = None
            if group_id:
                group_row = tx.execute(
                    "SELECT * FROM groups WHERE group_id=?",
                    (group_id,),
                ).fetchone()
                if group_row is not None:
                    group_data, _, _ = self._normalize_periods(
                        tx,
                        "groups",
                        "group_id",
                        group_id,
                        group_row,
                        today,
                        month,
                    )
                    group_update, reason, retry = self._reservation_update(
                        group_data,
                        tokens,
                        cost,
                        now_unix,
                        group=True,
                    )
                    if group_update is None:
                        return False, reason, retry

            self._update_counter_row(
                tx,
                "api_keys",
                "key_hash",
                key_hash,
                key_update,
            )
            if group_id and group_update is not None:
                self._update_counter_row(
                    tx,
                    "groups",
                    "group_id",
                    group_id,
                    group_update,
                )
        return True, None, 0

    @staticmethod
    def _post_request_updates(
        data: dict[str, Any],
        *,
        tokens: int,
        cost: float,
        reserved_tokens: int,
        reserved_cost: float,
        already_reserved: bool,
        daily_reset: bool,
        monthly_reset: bool,
        now_unix: int,
    ) -> dict[str, Any]:
        if already_reserved:
            daily_delta = tokens if daily_reset else tokens - reserved_tokens
            monthly_delta = cost if monthly_reset else cost - reserved_cost
            tpm_start = int(data.get("tpm_window_start", 0))
            if now_unix - tpm_start >= 60:
                tpm_start = now_unix
                tpm_count = tokens
            else:
                tpm_count = max(
                    0,
                    int(data.get("tpm_window_count", 0))
                    + tokens
                    - reserved_tokens,
                )
            return {
                "daily_tokens_used": max(
                    0,
                    int(data.get("daily_tokens_used", 0)) + daily_delta,
                ),
                "monthly_cost_used": round(
                    max(
                        0.0,
                        float(data.get("monthly_cost_used", 0.0))
                        + monthly_delta,
                    ),
                    12,
                ),
                "tpm_window_start": tpm_start,
                "tpm_window_count": tpm_count,
            }

        rpm_start = int(data.get("rpm_window_start", 0))
        if now_unix - rpm_start >= 60:
            rpm_start = now_unix
            rpm_count = 1
        else:
            rpm_count = int(data.get("rpm_window_count", 0)) + 1
        tpm_start = int(data.get("tpm_window_start", 0))
        if now_unix - tpm_start >= 60:
            tpm_start = now_unix
            tpm_count = tokens
        else:
            tpm_count = int(data.get("tpm_window_count", 0)) + tokens
        return {
            "rpm_window_start": rpm_start,
            "rpm_window_count": rpm_count,
            "tpm_window_start": tpm_start,
            "tpm_window_count": tpm_count,
            "daily_tokens_used": int(data.get("daily_tokens_used", 0)) + tokens,
            "monthly_cost_used": round(
                float(data.get("monthly_cost_used", 0.0)) + cost,
                12,
            ),
        }

    def _accumulate_period_record(
        self,
        tx: Any,
        entity_type: str,
        entity_id: str,
        period_type: str,
        period_value: str,
        tokens: int,
        cost: float,
        model: str,
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        row = tx.execute(
            """SELECT * FROM quota_records
               WHERE entity_type=? AND entity_id=?
                 AND period_type=? AND period_value=?""",
            (entity_type, entity_id, period_type, period_value),
        ).fetchone()
        updated = self._accumulate_quota(
            dict(row) if row is not None else None,
            tokens,
            cost,
            model,
            tokens_in,
            tokens_out,
        )
        tx.execute(
            """INSERT INTO quota_records
               (entity_type, entity_id, period_type, period_value,
                tokens_in, tokens_out, cost_usd, request_count, model_usage)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(entity_type, entity_id, period_type, period_value)
               DO UPDATE SET
                 tokens_in=excluded.tokens_in,
                 tokens_out=excluded.tokens_out,
                 cost_usd=excluded.cost_usd,
                 request_count=excluded.request_count,
                 model_usage=excluded.model_usage""",
            (
                entity_type,
                entity_id,
                period_type,
                period_value,
                updated["tokens_in"],
                updated["tokens_out"],
                updated["cost_usd"],
                updated["request_count"],
                updated["model_usage"],
            ),
        )

    async def increment_usage(
        self,
        key_hash: str,
        tokens: int,
        cost: float,
        model: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        *,
        _lua_already_incr: bool = False,
        _reserved_tokens: int = 0,
        _reserved_cost: float = 0.0,
    ) -> None:
        """Reconcile reservations and atomically append period usage records."""
        tokens, cost = self._validated_usage(tokens, cost)
        reserved_tokens, reserved_cost = self._validated_usage(
            _reserved_tokens,
            _reserved_cost,
        )
        now = self._quota_now()
        now_unix = int(now.timestamp())
        today = now.strftime("%Y-%m-%d")
        month = now.strftime("%Y-%m")

        with self._immediate_transaction() as tx:
            key_row = tx.execute(
                "SELECT * FROM api_keys WHERE key_hash=?",
                (key_hash,),
            ).fetchone()
            if key_row is None:
                return
            key_data, daily_reset, monthly_reset = self._normalize_periods(
                tx,
                "api_keys",
                "key_hash",
                key_hash,
                key_row,
                today,
                month,
            )
            key_updates = self._post_request_updates(
                key_data,
                tokens=tokens,
                cost=cost,
                reserved_tokens=reserved_tokens,
                reserved_cost=reserved_cost,
                already_reserved=_lua_already_incr,
                daily_reset=daily_reset,
                monthly_reset=monthly_reset,
                now_unix=now_unix,
            )
            self._update_counter_row(
                tx,
                "api_keys",
                "key_hash",
                key_hash,
                key_updates,
            )
            self._accumulate_period_record(
                tx,
                "key",
                key_hash,
                "daily",
                today,
                tokens,
                cost,
                model,
                tokens_in,
                tokens_out,
            )
            self._accumulate_period_record(
                tx,
                "key",
                key_hash,
                "monthly",
                month,
                tokens,
                cost,
                model,
                tokens_in,
                tokens_out,
            )

            group_id = str(key_data.get("group_id") or "")
            if not group_id:
                return
            group_row = tx.execute(
                "SELECT * FROM groups WHERE group_id=?",
                (group_id,),
            ).fetchone()
            if group_row is None:
                return
            group_data, group_daily_reset, group_monthly_reset = (
                self._normalize_periods(
                    tx,
                    "groups",
                    "group_id",
                    group_id,
                    group_row,
                    today,
                    month,
                )
            )
            group_updates = self._post_request_updates(
                group_data,
                tokens=tokens,
                cost=cost,
                reserved_tokens=reserved_tokens,
                reserved_cost=reserved_cost,
                already_reserved=_lua_already_incr,
                daily_reset=group_daily_reset,
                monthly_reset=group_monthly_reset,
                now_unix=now_unix,
            )
            self._update_counter_row(
                tx,
                "groups",
                "group_id",
                group_id,
                group_updates,
            )
            self._accumulate_period_record(
                tx,
                "group",
                group_id,
                "daily",
                today,
                tokens,
                cost,
                model,
                tokens_in,
                tokens_out,
            )
            self._accumulate_period_record(
                tx,
                "group",
                group_id,
                "monthly",
                month,
                tokens,
                cost,
                model,
                tokens_in,
                tokens_out,
            )

    @staticmethod
    def _release_updates(
        data: dict[str, Any],
        *,
        reserved_tokens: int,
        reserved_cost: float,
        daily_reset: bool,
        monthly_reset: bool,
        now_unix: int,
    ) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        if not daily_reset:
            updates["daily_tokens_used"] = max(
                0,
                int(data.get("daily_tokens_used", 0)) - reserved_tokens,
            )
        if not monthly_reset:
            updates["monthly_cost_used"] = round(
                max(
                    0.0,
                    float(data.get("monthly_cost_used", 0.0)) - reserved_cost,
                ),
                12,
            )
        tpm_start = int(data.get("tpm_window_start", 0))
        if now_unix - tpm_start < 60:
            updates["tpm_window_count"] = max(
                0,
                int(data.get("tpm_window_count", 0)) - reserved_tokens,
            )
        return updates

    async def release_reserved_usage(
        self,
        key_hash: str,
        *,
        reserved_tokens: int = 0,
        reserved_cost: float = 0.0,
    ) -> None:
        """Release key and group token/cost reservations without stale rollbacks."""
        reserved_tokens, reserved_cost = self._validated_usage(
            reserved_tokens,
            reserved_cost,
        )
        if reserved_tokens == 0 and reserved_cost == 0:
            return
        now = self._quota_now()
        now_unix = int(now.timestamp())
        today = now.strftime("%Y-%m-%d")
        month = now.strftime("%Y-%m")

        with self._immediate_transaction() as tx:
            key_row = tx.execute(
                "SELECT * FROM api_keys WHERE key_hash=?",
                (key_hash,),
            ).fetchone()
            if key_row is None:
                return
            key_data, daily_reset, monthly_reset = self._normalize_periods(
                tx,
                "api_keys",
                "key_hash",
                key_hash,
                key_row,
                today,
                month,
            )
            self._update_counter_row(
                tx,
                "api_keys",
                "key_hash",
                key_hash,
                self._release_updates(
                    key_data,
                    reserved_tokens=reserved_tokens,
                    reserved_cost=reserved_cost,
                    daily_reset=daily_reset,
                    monthly_reset=monthly_reset,
                    now_unix=now_unix,
                ),
            )
            group_id = str(key_data.get("group_id") or "")
            if not group_id:
                return
            group_row = tx.execute(
                "SELECT * FROM groups WHERE group_id=?",
                (group_id,),
            ).fetchone()
            if group_row is None:
                return
            group_data, group_daily_reset, group_monthly_reset = (
                self._normalize_periods(
                    tx,
                    "groups",
                    "group_id",
                    group_id,
                    group_row,
                    today,
                    month,
                )
            )
            self._update_counter_row(
                tx,
                "groups",
                "group_id",
                group_id,
                self._release_updates(
                    group_data,
                    reserved_tokens=reserved_tokens,
                    reserved_cost=reserved_cost,
                    daily_reset=group_daily_reset,
                    monthly_reset=group_monthly_reset,
                    now_unix=now_unix,
                ),
            )

    def _quota_period_rows(
        self,
        entity_type: str,
        entity_id: str,
        today: str,
        month: str,
    ) -> dict[str, dict[str, Any]]:
        """Return exact current records under both legacy and explicit keys."""
        rows = self.conn.fetchall(
            """SELECT * FROM quota_records
               WHERE entity_type=? AND entity_id=?
                 AND ((period_type='daily' AND period_value=?)
                   OR (period_type='monthly' AND period_value=?))""",
            (entity_type, entity_id, today, month),
        )
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            value = dict(row)
            period_type = str(row["period_type"])
            period_value = str(row["period_value"])
            result[period_type] = value
            result[f"{period_type}:{period_value}"] = value
        return result

    @staticmethod
    def _pricing_status(cost: float, model: str) -> str | None:
        """Resolve provenance for both rich and plain numeric accounting values.

        Non-stream bridge calls pass ``PricingCost`` with an attached status.
        Text streaming is finalized in the dispatcher and reaches this store as a
        plain float, so the status is reconstructed from the same runtime pricing
        configuration rather than silently collapsing free and unpriced requests.
        """
        explicit = getattr(cost, "pricing_status", None)
        if explicit in {"priced", "free", "unpriced"}:
            return explicit
        try:
            pricing = configured_model_pricing(model)
        except RuntimeError:
            return None
        if pricing is None:
            return "unpriced"
        return (
            "free"
            if pricing["prompt"] == 0.0 and pricing["completion"] == 0.0
            else "priced"
        )

    def _accumulate_quota(
        self,
        quota: dict | None,
        tokens: int,
        cost: float,
        model: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> dict:
        """Persist pricing provenance alongside numeric quota counters.

        Monthly cost remains numeric for compatibility. ``model_usage`` records
        explicit ``free_requests`` and ``unpriced_requests`` counters so missing
        pricing is not silently indistinguishable from a configured free model.
        """
        updated = super()._accumulate_quota(
            quota,
            tokens,
            cost,
            model,
            tokens_in,
            tokens_out,
        )
        pricing_status = self._pricing_status(cost, model)
        if pricing_status is None:
            return updated

        raw_model_usage = updated.get("model_usage", "{}")
        try:
            model_usage = (
                json.loads(raw_model_usage)
                if isinstance(raw_model_usage, str)
                else dict(raw_model_usage or {})
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            model_usage = {}

        entry: dict[str, Any]
        current = model_usage.get(model)
        if isinstance(current, dict):
            entry = current
        else:
            entry = {"in": tokens_in, "out": tokens_out}
        entry["pricing_status"] = pricing_status
        counter_name = f"{pricing_status}_requests"
        entry[counter_name] = int(entry.get(counter_name, 0)) + 1
        model_usage[model] = entry
        updated["model_usage"] = json.dumps(model_usage, ensure_ascii=False)
        return updated


# Preserve constants, helper functions and data models imported from this module
# before the implementation was split. Only the configured class is overridden.
for _name in dir(_impl):
    if _name.startswith("__") or _name == "SQLiteStore":
        continue
    if _name not in globals():
        globals()[_name] = getattr(_impl, _name)

__all__ = [
    name
    for name in globals()
    if not name.startswith("_") and name not in {"Path", "Any", "Iterator"}
]
