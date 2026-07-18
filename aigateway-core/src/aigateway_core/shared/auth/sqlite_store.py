"""SQLite-backed auth store for API keys, groups, and quotas.

Replaces Redis-backed KeyStore/GroupStore with a persistent file-based
database. All data survives container rebuilds when the DB file is
mounted as a volume.

Schema:
- api_keys: key metadata + limits + runtime counters
- quota_records: per-day/per-month usage tracking (key + group)
- groups: group metadata + shared limits
- group_members: many-to-many key↔group
- meta: schema versioning + migration state

Config: AI_GATEWAY_AUTH_DB_PATH env var (default /app/data/auth.db)
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import string
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Schema ──────────────────────────────────────────────────────────

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash TEXT PRIMARY KEY,
    key_id TEXT,
    key_prefix TEXT,
    user_id TEXT,
    status TEXT DEFAULT 'active',
    created_at TEXT,
    last_used_at TEXT,
    group_id TEXT DEFAULT '',
    cache_scope TEXT DEFAULT 'group',
    daily_tokens_limit INTEGER DEFAULT 1000000,
    daily_tokens_used INTEGER DEFAULT 0,
    monthly_cost_limit REAL DEFAULT 50.0,
    monthly_cost_used REAL DEFAULT 0.0,
    rate_limit_rpm INTEGER DEFAULT 60,
    rate_limit_tpm INTEGER DEFAULT 100000,
    rpm_window_start INTEGER DEFAULT 0,
    rpm_window_count INTEGER DEFAULT 0,
    tpm_window_start INTEGER DEFAULT 0,
    tpm_window_count INTEGER DEFAULT 0,
    is_admin INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS quota_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL CHECK(entity_type IN ('key','group')),
    entity_id TEXT NOT NULL,
    period_type TEXT NOT NULL CHECK(period_type IN ('daily','monthly')),
    period_value TEXT NOT NULL,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    request_count INTEGER DEFAULT 0,
    model_usage TEXT DEFAULT '{}',
    UNIQUE(entity_type, entity_id, period_type, period_value)
);

CREATE TABLE IF NOT EXISTS groups (
    group_id TEXT PRIMARY KEY,
    name TEXT UNIQUE,
    status TEXT DEFAULT 'active',
    created_at TEXT,
    updated_at TEXT,
    daily_tokens_limit INTEGER DEFAULT 1000000,
    daily_tokens_used INTEGER DEFAULT 0,
    monthly_cost_limit REAL DEFAULT 50.0,
    monthly_cost_used REAL DEFAULT 0.0,
    rate_limit_rpm INTEGER DEFAULT 60,
    rate_limit_tpm INTEGER DEFAULT 100000,
    rpm_window_start INTEGER DEFAULT 0,
    rpm_window_count INTEGER DEFAULT 0,
    tpm_window_start INTEGER DEFAULT 0,
    tpm_window_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id TEXT NOT NULL REFERENCES groups(group_id),
    key_hash TEXT NOT NULL REFERENCES api_keys(key_hash),
    PRIMARY KEY (group_id, key_hash)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS request_cost_ledger (
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
);

CREATE INDEX IF NOT EXISTS idx_ledger_ts ON request_cost_ledger(ts_unix);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON request_cost_ledger(user_id);
CREATE INDEX IF NOT EXISTS idx_ledger_group ON request_cost_ledger(group_id);
CREATE INDEX IF NOT EXISTS idx_ledger_model ON request_cost_ledger(model);
"""


# ── Helpers ─────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_unix() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _hash_key(key_value: str) -> str:
    import hashlib
    return hashlib.sha256(key_value.encode("utf-8")).hexdigest()[:16]


def _prefix_key(key_value: str) -> str:
    return key_value[:8]


def _slugify(name: str) -> str:
    s = name.strip().lower()
    out: list[str] = []
    prev_dash = False
    for ch in s:
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        else:
            if not prev_dash:
                out.append("-")
                prev_dash = True
    return "".join(out).strip("-")


def _quota_base() -> dict[str, str]:
    return {
        "tokens_in": "0",
        "tokens_out": "0",
        "cost_usd": "0.0",
        "request_count": "0",
        "model_usage": "{}",
    }


# ── Connection helper ───────────────────────────────────────────────

class _Conn:
    """Thin wrapper around a sqlite3.Connection with WAL mode + row_factory."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        # Ensure parent directory exists
        parent = os.path.dirname(db_path) or "."
        os.makedirs(parent, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        if getattr(self._local, "conn", None) is None:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            # synchronous=NORMAL: WAL 模式下崩溃安全(仅可能丢最后几个事务),避免每次 commit fsync 阻塞事件循环
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def execute(self, sql: str, params=(), **kwargs):
        return self._connect().execute(sql, params, **kwargs)

    def executemany(self, sql: str, rows):
        return self._connect().executemany(sql, rows)

    def fetchone(self, sql: str, params=()):
        return self._connect().execute(sql, params).fetchone()

    def fetchall(self, sql: str, params=()):
        return self._connect().execute(sql, params).fetchall()

    def commit(self):
        self._connect().commit()

    @contextmanager
    def transaction(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn:
            conn.close()
            self._local.conn = None


# ── SQLiteStore ─────────────────────────────────────────────────────

class SQLiteStore:
    """Unified auth store backed by SQLite.

    Replaces both KeyStore and GroupStore with a single class that
    provides the same public interface so existing callers don't need
    to change.
    """

    DEFAULT_DAILY_TOKENS = 1_000_000
    DEFAULT_MONTHLY_COST = 50.0
    DEFAULT_RATE_LIMIT_RPM = 60
    DEFAULT_RATE_LIMIT_TPM = 100_000
    DEFAULT_GROUP_ID = "grp-default"
    DEFAULT_GROUP_NAME = "default"

    def __init__(self, db_path: Optional[str] = None):
        path = db_path or os.environ.get("AI_GATEWAY_AUTH_DB_PATH")
        if path is None:
            # 默认使用项目根目录下的 data/auth.db。
            # Docker 容器内 CWD=/app → /app/data/auth.db (bind mount ./data:/app/data)
            # 本地运行 CWD=项目根 → ./data/auth.db
            path = "data/auth.db"
        self.db_path = path
        self.conn = _Conn(path)
        self._init_schema()

    def _init_schema(self):
        # Execute each CREATE TABLE separately (sqlite3.execute doesn't accept multi-statement)
        conn = self.conn._connect()
        for stmt in SCHEMA_SQL.split(';'):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)
        conn.commit()
        # Check migration version
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO meta VALUES (?, ?)", ("schema_version", "1")
            )
            conn.commit()

    # ── Internal helpers ──────────────────────────────────────────

    def _row_to_dict(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        # Convert is_admin from int to bool
        if "is_admin" in d:
            d["is_admin"] = bool(d["is_admin"])
        return d

    def _api_key_row(self, kh: str) -> sqlite3.Row | None:
        return self.conn.fetchone(
            "SELECT * FROM api_keys WHERE key_hash=?", (kh,)
        )

    def _lookup_by_prefix(self, prefix: str) -> str | None:
        row = self.conn.fetchone(
            "SELECT key_hash FROM api_keys WHERE key_prefix=?", (prefix,)
        )
        return row["key_hash"] if row else None

    def _lookup_by_id(self, key_id: str) -> list[str]:
        rows = self.conn.fetchall(
            "SELECT key_hash FROM api_keys WHERE key_id=?", (key_id,)
        )
        return [r["key_hash"] for r in rows]

    def _find_by_id_scan(self, key_id: str) -> list[str]:
        """Fallback scan all keys for matching key_id (used by KeyStore)."""
        rows = self.conn.fetchall(
            "SELECT key_hash FROM api_keys WHERE key_id=?", (key_id,)
        )
        return [r["key_hash"] for r in rows]

    def _duplicate_user_key(self, user_id: str) -> bool:
        row = self.conn.fetchone(
            "SELECT key_hash FROM api_keys WHERE user_id=? AND status='active'",
            (user_id,),
        )
        return row is not None

    def _quota_period_rows(self, entity_type: str, entity_id: str,
                           today: str, month: str) -> dict[str, dict[str, str]]:
        rows = self.conn.fetchall(
            "SELECT * FROM quota_records WHERE entity_type=? AND entity_id=?",
            (entity_type, entity_id),
        )
        result = {}
        for r in rows:
            key = f"{r['period_type']}:{r['period_value']}"
            result[key] = dict(r)
        return result

    def _upsert_quota_record(self, entity_type: str, entity_id: str,
                             period_type: str, period_value: str,
                             updates: dict[str, str]):
        """INSERT OR REPLACE quota record."""
        base = _quota_base()
        base.update(updates)
        self.conn.execute(
            """INSERT OR REPLACE INTO quota_records
               (entity_type, entity_id, period_type, period_value,
                tokens_in, tokens_out, cost_usd, request_count, model_usage)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entity_type, entity_id, period_type, period_value,
                base["tokens_in"], base["tokens_out"], base["cost_usd"],
                base["request_count"], base["model_usage"],
            ),
        )

    def _accumulate_quota(self, quota: dict | None, tokens: int,
                          cost: float, model: str,
                          tokens_in: int = 0, tokens_out: int = 0) -> dict:
        if not quota:
            quota = _quota_base()
        quota["tokens_in"] = str(int(quota.get("tokens_in", "0")) + tokens_in)
        quota["tokens_out"] = str(int(quota.get("tokens_out", "0")) + tokens_out)
        quota["cost_usd"] = str(float(quota.get("cost_usd", "0.0")) + cost)
        quota["request_count"] = str(int(quota.get("request_count", "0")) + 1)
        mu_raw = quota.get("model_usage", "{}")
        try:
            model_usage = json.loads(mu_raw) if isinstance(mu_raw, str) else mu_raw
        except (json.JSONDecodeError, TypeError):
            model_usage = {}
        entry = model_usage.get(model, {"in": 0, "out": 0})
        if isinstance(entry, dict):
            entry["in"] = entry.get("in", 0) + tokens_in
            entry["out"] = entry.get("out", 0) + tokens_out
        else:
            entry = {"in": tokens_in, "out": tokens_out}
        model_usage[model] = entry
        quota["model_usage"] = json.dumps(model_usage, ensure_ascii=False)
        return quota

    # ── KeyStore-compatible API ───────────────────────────────────

    async def validate(self, key: str) -> Optional[dict[str, Any]]:
        """Validate API Key. Auto-reseed from config if empty."""
        key_hash = _hash_key(key)
        row = self._api_key_row(key_hash)

        if row is None:
            # Try auto-reseed
            seeded = await self._try_auto_reseed()
            if seeded:
                row = self._api_key_row(key_hash)

        if row is None:
            logger.warning("API Key hash=%s 未找到", key_hash)
            return None

        data = self._row_to_dict(row)
        status = data.get("status", "")
        if status == "revoked":
            from aigateway_core.shared.exceptions import AuthError
            raise AuthError(f"API key '{data.get('key_id')}' has been revoked")
        if status == "suspended":
            from aigateway_core.shared.exceptions import AuthError
            raise AuthError(f"API key '{data.get('key_id')}' is suspended")

        # Normalize is_admin
        data["is_admin"] = bool(data.get("is_admin", 0))
        # Update last_used_at
        now = _now_iso()
        data["last_used_at"] = now
        self.conn.execute(
            "UPDATE api_keys SET last_used_at=? WHERE key_hash=?",
            (now, key_hash),
        )
        self.conn.commit()
        return data

    async def _try_auto_reseed(self) -> bool:
        """If no keys exist, seed from config.yaml."""
        row = self.conn.fetchone("SELECT 1 FROM api_keys LIMIT 1")
        if row:
            return False
        try:
            from aigateway_api.app_state import get_state
            config_manager = getattr(get_state(), "config_manager", None)
            if config_manager:
                auth_config = config_manager.get("auth", {})
                keys_config = auth_config.get("api_keys", [])
                if keys_config:
                    await self.seed_from_config(keys_config)
                    logger.info("API Keys re-seeded from config.yaml")
                    return True
        except Exception as exc:
            logger.warning("Auto-reseed failed: %s", exc)
        return False

    async def create(
        self,
        user_id: str,
        quotas: Optional[Dict[str, Any]] = None,
        group_id: str = "",
        cache_scope: str = "group",
    ) -> Dict[str, Any]:
        if not user_id:
            raise ValueError("user_id is required")

        raw_key = f"gw-{''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))}"
        key_hash = _hash_key(raw_key)
        key_prefix = _prefix_key(raw_key)
        now_iso = _now_iso()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month = datetime.now(timezone.utc).strftime("%Y-%m")

        q = quotas or {}
        daily_tokens = q.get("daily_tokens", self.DEFAULT_DAILY_TOKENS)
        monthly_cost = q.get("monthly_cost", self.DEFAULT_MONTHLY_COST)
        rate_rpm = q.get("rate_limit_rpm", self.DEFAULT_RATE_LIMIT_RPM)
        rate_tpm = q.get("rate_limit_tpm", self.DEFAULT_RATE_LIMIT_TPM)

        key_id = f"key_{uuid.uuid4().hex[:8]}"

        # Check duplicate
        if self._duplicate_user_key(user_id):
            raise ValueError(f"用户 '{user_id}' 已存在活跃 Key: existing")

        with self.conn.transaction() as tx:
            tx.execute(
                """INSERT INTO api_keys
                   (key_hash, key_id, key_prefix, user_id, status,
                    created_at, last_used_at, group_id, cache_scope,
                    daily_tokens_limit, daily_tokens_used,
                    monthly_cost_limit, monthly_cost_used,
                    rate_limit_rpm, rate_limit_tpm,
                    rpm_window_start, rpm_window_count,
                    tpm_window_start, tpm_window_count)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    key_hash, key_id, key_prefix, user_id, "active",
                    now_iso, "", group_id or "", cache_scope or "group",
                    daily_tokens, 0,
                    monthly_cost, 0.0,
                    rate_rpm, rate_tpm,
                    _now_unix(), 0,
                    _now_unix(), 0,
                ),
            )
            qb = _quota_base()
            tx.execute(
                """INSERT INTO quota_records
                   (entity_type, entity_id, period_type, period_value,
                    tokens_in, tokens_out, cost_usd, request_count, model_usage)
                   VALUES ('key', ?, 'daily', ?, ?, ?, ?, ?, ?)""",
                (key_hash, today, qb["tokens_in"], qb["tokens_out"],
                 qb["cost_usd"], qb["request_count"], qb["model_usage"]),
            )
            tx.execute(
                """INSERT INTO quota_records
                   (entity_type, entity_id, period_type, period_value,
                    tokens_in, tokens_out, cost_usd, request_count, model_usage)
                   VALUES ('key', ?, 'monthly', ?, ?, ?, ?, ?, ?)""",
                (key_hash, month, qb["tokens_in"], qb["tokens_out"],
                 qb["cost_usd"], qb["request_count"], qb["model_usage"]),
            )
            if group_id:
                tx.execute(
                    "INSERT OR IGNORE INTO group_members (group_id, key_hash) VALUES (?, ?)",
                    (group_id, key_hash),
                )
                # Ensure group exists
                g = tx.execute(
                    "SELECT group_id FROM groups WHERE group_id=?", (group_id,)
                ).fetchone()
                if g is None:
                    # Create default group if referenced but missing
                    slug = _slugify(self.DEFAULT_GROUP_NAME)
                    gid = f"grp-{slug}"
                    tx.execute(
                        """INSERT INTO groups (group_id, name, status, created_at, updated_at)
                           VALUES (?,?,?,?,?)""",
                        (gid, self.DEFAULT_GROUP_NAME, "active", now_iso, now_iso),
                    )
                    tx.execute(
                        "INSERT OR REPLACE INTO meta VALUES (?, ?)",
                        ("default_group_id", gid),
                    )

        # Publish pub/sub events (best-effort, non-blocking)
        try:
            await self.publish("keys:sync", {
                "event_type": "key_created",
                "key_id": key_id,
                "user_id": user_id,
                "timestamp": now_iso,
            })
        except Exception:
            pass

        logger.info("API Key 创建成功: user_id=%s, key_id=%s, key_hash=%s", user_id, key_id, key_hash)
        return {
            "id": key_id,
            "key": raw_key,
            "key_prefix": key_prefix,
            "user_id": user_id,
            "group_id": group_id or "",
            "cache_scope": cache_scope or "group",
            "created_at": now_iso,
            "status": "active",
            "quotas": {
                "daily_tokens": daily_tokens,
                "monthly_cost": monthly_cost,
                "rate_limit_rpm": rate_rpm,
                "rate_limit_tpm": rate_tpm,
            },
        }

    async def seed_from_config(self, keys_config: List[Dict[str, Any]]) -> int:
        if not keys_config:
            return 0

        imported = 0
        now_iso = _now_iso()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month = datetime.now(timezone.utc).strftime("%Y-%m")

        for cfg in keys_config:
            raw_key = cfg.get("key", "")
            user_id = cfg.get("user_id", "")
            if not raw_key or not user_id:
                logger.warning("config api_keys 条目缺少 key 或 user_id，跳过: %s", cfg)
                continue

            key_hash = _hash_key(raw_key)
            key_prefix = raw_key[:8]
            quotas = cfg.get("quotas", {})
            is_admin = bool(cfg.get("is_admin", False))
            cfg_group = cfg.get("group") or ""
            if cfg_group:
                slug = _slugify(cfg_group.replace("grp-", ""))
                cfg_group = f"grp-{slug}"

            with self.conn.transaction() as tx:
                existing = tx.execute(
                    "SELECT * FROM api_keys WHERE key_hash=?", (key_hash,)
                ).fetchone()

                if existing:
                    # Only update structural fields, preserve runtime-modified quotas
                    existing_d = dict(existing)
                    existing_d["user_id"] = user_id
                    existing_d["status"] = "active"
                    existing_d["is_admin"] = int(is_admin)
                    if cfg_group:
                        existing_d["group_id"] = cfg_group
                    if "group_id" not in existing_d or not existing_d["group_id"]:
                        existing_d["group_id"] = ""
                    if "cache_scope" not in existing_d or not existing_d["cache_scope"]:
                        existing_d["cache_scope"] = "group"
                    if "daily_tokens_limit" not in existing_d or not existing_d["daily_tokens_limit"]:
                        existing_d["daily_tokens_limit"] = str(quotas.get("daily_tokens", self.DEFAULT_DAILY_TOKENS))
                    if "monthly_cost_limit" not in existing_d or not existing_d["monthly_cost_limit"]:
                        existing_d["monthly_cost_limit"] = str(quotas.get("monthly_cost", self.DEFAULT_MONTHLY_COST))
                    if "rate_limit_rpm" not in existing_d or not existing_d["rate_limit_rpm"]:
                        existing_d["rate_limit_rpm"] = str(quotas.get("rate_limit_rpm", self.DEFAULT_RATE_LIMIT_RPM))
                    if "rate_limit_tpm" not in existing_d or not existing_d["rate_limit_tpm"]:
                        existing_d["rate_limit_tpm"] = str(quotas.get("rate_limit_tpm", self.DEFAULT_RATE_LIMIT_TPM))

                    tx.execute(
                        """UPDATE api_keys SET user_id=?, status=?, is_admin=?,
                           group_id=?, cache_scope=?,
                           daily_tokens_limit=?, monthly_cost_limit=?,
                           rate_limit_rpm=?, rate_limit_tpm=?
                           WHERE key_hash=?""",
                        (
                            user_id, "active", int(is_admin),
                            existing_d["group_id"], existing_d["cache_scope"],
                            existing_d["daily_tokens_limit"], existing_d["monthly_cost_limit"],
                            existing_d["rate_limit_rpm"], existing_d["rate_limit_tpm"],
                            key_hash,
                        ),
                    )
                    logger.info("API Key 已更新: user_id=%s, key_hash=%s", user_id, key_hash)
                else:
                    tx.execute(
                        """INSERT INTO api_keys
                           (key_hash, key_id, key_prefix, user_id, status,
                            created_at, last_used_at, group_id, cache_scope,
                            daily_tokens_limit, daily_tokens_used,
                            monthly_cost_limit, monthly_cost_used,
                            rate_limit_rpm, rate_limit_tpm,
                            rpm_window_start, rpm_window_count,
                            tpm_window_start, tpm_window_count, is_admin)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            key_hash, f"key_{uuid.uuid4().hex[:8]}", key_prefix,
                            user_id, "active", now_iso, "",
                            cfg_group, "group",
                            quotas.get("daily_tokens", self.DEFAULT_DAILY_TOKENS), 0,
                            quotas.get("monthly_cost", self.DEFAULT_MONTHLY_COST), 0.0,
                            quotas.get("rate_limit_rpm", self.DEFAULT_RATE_LIMIT_RPM),
                            quotas.get("rate_limit_tpm", self.DEFAULT_RATE_LIMIT_TPM),
                            _now_unix(), 0, _now_unix(), 0, int(is_admin),
                        ),
                    )
                    qb = _quota_base()
                    tx.execute(
                        """INSERT INTO quota_records
                           (entity_type, entity_id, period_type, period_value,
                            tokens_in, tokens_out, cost_usd, request_count, model_usage)
                           VALUES ('key', ?, 'daily', ?, ?, ?, ?, ?, ?)""",
                        (key_hash, today, qb["tokens_in"], qb["tokens_out"],
                         qb["cost_usd"], qb["request_count"], qb["model_usage"]),
                    )
                    tx.execute(
                        """INSERT INTO quota_records
                           (entity_type, entity_id, period_type, period_value,
                            tokens_in, tokens_out, cost_usd, request_count, model_usage)
                           VALUES ('key', ?, 'monthly', ?, ?, ?, ?, ?, ?)""",
                        (key_hash, month, qb["tokens_in"], qb["tokens_out"],
                         qb["cost_usd"], qb["request_count"], qb["model_usage"]),
                    )
                    logger.info("API Key 已创建: user_id=%s, key_hash=%s, is_admin=%s", user_id, key_hash, is_admin)

                # Handle group membership
                if cfg_group:
                    # Ensure group exists first
                    g = tx.execute(
                        "SELECT group_id FROM groups WHERE group_id=?", (cfg_group,)
                    ).fetchone()
                    if g is None:
                        slug = _slugify(cfg_group.replace("grp-", ""))
                        gid = f"grp-{slug}"
                        tx.execute(
                            """INSERT INTO groups (group_id, name, status, created_at, updated_at)
                               VALUES (?,?,?,?,?)""",
                            (gid, slug, "active", now_iso, now_iso),
                        )
                    # Now insert group member
                    tx.execute(
                        "INSERT OR IGNORE INTO group_members (group_id, key_hash) VALUES (?, ?)",
                        (cfg_group, key_hash),
                    )

            imported += 1

        return imported

    async def revoke(self, key_id: str) -> bool:
        if not key_id.startswith("key_"):
            raise ValueError("Invalid key_id format, should be key_xxx")

        hashes = self._lookup_by_id(key_id)
        if not hashes:
            logger.warning("未找到 key_id=%s 对应的 Key 记录", key_id)
            return False

        now_iso = _now_iso()
        user_id = ""
        key_prefix = ""
        for kh in hashes:
            self.conn.execute(
                "UPDATE api_keys SET status='revoked', last_used_at=? WHERE key_hash=?",
                (now_iso, kh),
            )
            # Clean up group membership when revoking
            self.conn.execute(
                "DELETE FROM group_members WHERE key_hash=?", (kh,)
            )
            row = self._api_key_row(kh)
            if row:
                d = dict(row)
                user_id = d.get("user_id", "")
                key_prefix = d.get("key_prefix", "")

        self.conn.commit()

        try:
            await self.publish("keys:sync", {
                "event_type": "key_revoked",
                "key_id": key_id,
                "user_id": user_id,
                "timestamp": now_iso,
            })
        except Exception:
            pass

        logger.info("API Key 已撤销: key_id=%s", key_id)
        return True

    async def delete_permanently(self, key_id: str) -> bool:
        if not key_id.startswith("key_"):
            raise ValueError("Invalid key_id format, should be key_xxx")

        hashes = self._lookup_by_id(key_id)
        if not hashes:
            logger.warning("未找到 key_id=%s 对应的 Key 记录", key_id)
            return False

        with self.conn.transaction() as tx:
            for kh in hashes:
                row = tx.execute(
                    "SELECT key_prefix FROM api_keys WHERE key_hash=?", (kh,)
                ).fetchone()
                if row:
                    tx.execute("DELETE FROM quota_records WHERE entity_type='key' AND entity_id=?", (kh,))
                    tx.execute("DELETE FROM group_members WHERE key_hash=?", (kh,))
                    tx.execute("DELETE FROM api_keys WHERE key_hash=?", (kh,))
                    logger.info("API Key 已永久删除: key_id=%s, key_hash=%s", key_id, kh)

        try:
            await self.publish("keys:sync", {
                "event_type": "key_deleted",
                "key_id": key_id,
                "user_id": "",
                "timestamp": _now_iso(),
            })
        except Exception:
            pass

        return True

    async def ensure_seeded(self, keys_config: List[Dict[str, Any]]) -> int:
        row = self.conn.fetchone("SELECT 1 FROM api_keys LIMIT 1")
        if row:
            return 0
        logger.info("API Keys re-seeded from config.yaml")
        return await self.seed_from_config(keys_config)

    # ── Quota check & accumulation ────────────────────────────────

    async def check_quota(
        self,
        key_hash: str,
        tokens: int,
        cost: float,
    ) -> Tuple[bool, Optional[str], int]:
        """Atomic check+reserve via transaction.

        Checks RPM/TPM/daily/monthly for both key and group levels.
        Bumps counters atomically inside a transaction.
        """
        row = self._api_key_row(key_hash)
        if not row:
            return False, "API Key does not exist", 0

        data = self._row_to_dict(row)
        group_id = data.get("group_id") or ""
        now_unix = _now_unix()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month = datetime.now(timezone.utc).strftime("%Y-%m")

        # Check key-level
        ok, reason, retry = self._check_key_dims(data, tokens, cost, now_unix)
        if not ok:
            return False, reason, retry

        # Check group-level
        if group_id:
            g_row = self.conn.fetchone(
                "SELECT * FROM groups WHERE group_id=?", (group_id,)
            )
            if g_row:
                g_data = dict(g_row)
                gok, greason, gretry = self._check_group_dims(g_data, tokens, cost, now_unix)
                if not gok:
                    return False, f"Group {greason}", gretry

        # All checks passed — bump counters atomically
        with self.conn.transaction() as tx:
            # Key-level: RPM window reset + increment
            rpm_ws = int(data.get("rpm_window_start", 0))
            rpm_wc = int(data.get("rpm_window_count", 0))
            if now_unix - rpm_ws >= 60:
                rpm_ws = now_unix
                rpm_wc = 1
            else:
                rpm_wc += 1
            tpm_ws = int(data.get("tpm_window_start", 0))
            tpm_wc = int(data.get("tpm_window_count", 0))
            if now_unix - tpm_ws >= 60:
                tpm_ws = now_unix
                tpm_wc = tokens
            else:
                tpm_wc += tokens

            daily_used = int(data.get("daily_tokens_used", 0)) + tokens
            monthly_used = round(float(data.get("monthly_cost_used", 0.0)) + cost, 4)

            tx.execute(
                """UPDATE api_keys SET rpm_window_start=?, rpm_window_count=?,
                   tpm_window_start=?, tpm_window_count=?,
                   daily_tokens_used=?, monthly_cost_used=?
                   WHERE key_hash=?""",
                (rpm_ws, rpm_wc, tpm_ws, tpm_wc, daily_used, monthly_used, key_hash),
            )

            # Group-level bumps
            if group_id:
                g_row2 = tx.execute(
                    "SELECT * FROM groups WHERE group_id=?", (group_id,)
                ).fetchone()
                if g_row2:
                    gd = dict(g_row2)
                    grpm_ws = int(gd.get("rpm_window_start", 0))
                    grpm_wc = int(gd.get("rpm_window_count", 0))
                    if now_unix - grpm_ws >= 60:
                        grpm_ws = now_unix
                        grpm_wc = 1
                    else:
                        grpm_wc += 1
                    gtpm_ws = int(gd.get("tpm_window_start", 0))
                    gtpm_wc = int(gd.get("tpm_window_count", 0))
                    if now_unix - gtpm_ws >= 60:
                        gtpm_ws = now_unix
                        gtpm_wc = tokens
                    else:
                        gtpm_wc += tokens
                    gdaily_used = int(gd.get("daily_tokens_used", 0)) + tokens
                    gmonthly_used = round(float(gd.get("monthly_cost_used", 0.0)) + cost, 4)
                    tx.execute(
                        """UPDATE groups SET rpm_window_start=?, rpm_window_count=?,
                           tpm_window_start=?, tpm_window_count=?,
                           daily_tokens_used=?, monthly_cost_used=?
                           WHERE group_id=?""",
                        (grpm_ws, grpm_wc, gtpm_ws, gtpm_wc, gdaily_used, gmonthly_used, group_id),
                    )

        return True, None, 0

    @staticmethod
    def _check_key_dims(data: dict, tokens: int, cost: float, now_unix: int
                        ) -> Tuple[bool, Optional[str], int]:
        resets: dict = {}
        rpm_limit = int(data.get("rate_limit_rpm", SQLiteStore.DEFAULT_RATE_LIMIT_RPM))
        rpm_ws = int(data.get("rpm_window_start", 0))
        rpm_wc = int(data.get("rpm_window_count", 0))
        if now_unix - rpm_ws >= 60:
            resets["rpm_window_start"] = str(now_unix)
            resets["rpm_window_count"] = "0"
            rpm_wc = 0
            rpm_ws = now_unix
        elif rpm_wc >= rpm_limit:
            return False, f"RPM limit exceeded: {rpm_wc}/{rpm_limit}", rpm_ws + 60 - now_unix

        tpm_limit = int(data.get("rate_limit_tpm", SQLiteStore.DEFAULT_RATE_LIMIT_TPM))
        tpm_ws = int(data.get("tpm_window_start", 0))
        tpm_wc = int(data.get("tpm_window_count", 0))
        if now_unix - tpm_ws >= 60:
            resets["tpm_window_start"] = str(now_unix)
            resets["tpm_window_count"] = "0"
            tpm_wc = 0
            tpm_ws = now_unix
        elif tpm_wc + tokens > tpm_limit:
            return False, f"TPM limit exceeded: {tpm_wc+tokens}/{tpm_limit}", tpm_ws + 60 - now_unix

        daily_limit = int(data.get("daily_tokens_limit", SQLiteStore.DEFAULT_DAILY_TOKENS))
        daily_used = int(data.get("daily_tokens_used", 0))
        if daily_used + tokens > daily_limit:
            return False, f"Daily token limit exceeded: {daily_used}/{daily_limit}", 0

        monthly_limit = float(data.get("monthly_cost_limit", SQLiteStore.DEFAULT_MONTHLY_COST))
        monthly_used = float(data.get("monthly_cost_used", 0.0))
        if monthly_used + cost > monthly_limit:
            return False, f"Monthly cost limit exceeded: ${monthly_used:.2f}/${monthly_limit:.2f}", 0

        return True, None, 0

    @staticmethod
    def _check_group_dims(data: dict, tokens: int, cost: float, now_unix: int
                          ) -> Tuple[bool, Optional[str], int]:
        rpm_limit = int(data.get("rate_limit_rpm", SQLiteStore.DEFAULT_RATE_LIMIT_RPM))
        rpm_ws = int(data.get("rpm_window_start", 0))
        rpm_wc = int(data.get("rpm_window_count", 0))
        if now_unix - rpm_ws >= 60:
            rpm_ws = now_unix
            rpm_wc = 0
        elif rpm_wc >= rpm_limit:
            return False, f"RPM limit exceeded: {rpm_wc}/{rpm_limit}", rpm_ws + 60 - now_unix

        tpm_limit = int(data.get("rate_limit_tpm", SQLiteStore.DEFAULT_RATE_LIMIT_TPM))
        tpm_ws = int(data.get("tpm_window_start", 0))
        tpm_wc = int(data.get("tpm_window_count", 0))
        if now_unix - tpm_ws >= 60:
            tpm_ws = now_unix
            tpm_wc = 0
        elif tpm_wc + tokens > tpm_limit:
            return False, f"TPM limit exceeded: {tpm_wc+tokens}/{tpm_limit}", tpm_ws + 60 - now_unix

        daily_limit = int(data.get("daily_tokens_limit", SQLiteStore.DEFAULT_DAILY_TOKENS))
        daily_used = int(data.get("daily_tokens_used", 0))
        if daily_used + tokens > daily_limit:
            return False, f"Group daily token limit exceeded: {daily_used}/{daily_limit}", 0

        monthly_limit = float(data.get("monthly_cost_limit", SQLiteStore.DEFAULT_MONTHLY_COST))
        monthly_used = float(data.get("monthly_cost_used", 0.0))
        if monthly_used + cost > monthly_limit:
            return False, f"Group monthly cost limit exceeded: ${monthly_used:.2f}/${monthly_limit:.2f}", 0

        return True, None, 0

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
        """Post-request usage reconciliation.

        When called after Lua-equivalent check_quota (which already bumped
        counters), applies only the delta between actual and reserved.
        Otherwise bumps from scratch.
        """
        row = self._api_key_row(key_hash)
        if not row:
            return
        data = self._row_to_dict(row)
        group_id = data.get("group_id") or ""
        now_unix = _now_unix()

        with self.conn.transaction() as tx:
            # Key-level counter reconciliation
            if _lua_already_incr:
                token_delta = tokens - _reserved_tokens
                cost_delta = cost - _reserved_cost
                if token_delta != 0 or cost_delta != 0:
                    daily_used = max(0, int(data.get("daily_tokens_used", "0")) + token_delta)
                    tpm_wc = int(data.get("tpm_window_count", "0"))
                    tpm_ws = int(data.get("tpm_window_start", 0))
                    if now_unix - tpm_ws < 60:
                        tpm_wc = max(0, tpm_wc + token_delta)
                    monthly_used = max(0.0, float(data.get("monthly_cost_used", "0.0")) + cost_delta)
                    tx.execute(
                        """UPDATE api_keys SET daily_tokens_used=?, tpm_window_count=?, monthly_cost_used=?
                           WHERE key_hash=?""",
                        (daily_used, tpm_wc, monthly_used, key_hash),
                    )
            else:
                rpm_ws = int(data.get("rpm_window_start", 0))
                rpm_wc = int(data.get("rpm_window_count", 0)) + 1
                if now_unix - rpm_ws >= 60:
                    rpm_ws = now_unix
                    rpm_wc = 1
                tpm_ws = int(data.get("tpm_window_start", 0))
                tpm_wc = int(data.get("tpm_window_count", 0)) + tokens
                if now_unix - tpm_ws >= 60:
                    tpm_ws = now_unix
                    tpm_wc = tokens
                daily_used = int(data.get("daily_tokens_used", "0")) + tokens
                monthly_used = round(float(data.get("monthly_cost_used", "0.0")) + cost, 4)
                tx.execute(
                    """UPDATE api_keys SET rpm_window_start=?, rpm_window_count=?,
                       tpm_window_start=?, tpm_window_count=?,
                       daily_tokens_used=?, monthly_cost_used=?
                       WHERE key_hash=?""",
                    (rpm_ws, rpm_wc, tpm_ws, tpm_wc, daily_used, monthly_used, key_hash),
                )

            # Quota period records
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            month = datetime.now(timezone.utc).strftime("%Y-%m")
            dq = self._quota_period_rows("key", key_hash, today, month)
            daily_q = self._accumulate_quota(dq.get("daily"), tokens, cost, model, tokens_in, tokens_out)
            monthly_q = self._accumulate_quota(dq.get("monthly"), tokens, cost, model, tokens_in, tokens_out)
            self._upsert_quota_record("key", key_hash, "daily", today, daily_q)
            self._upsert_quota_record("key", key_hash, "monthly", month, monthly_q)

            # Group-level
            if group_id:
                g_row = tx.execute(
                    "SELECT * FROM groups WHERE group_id=?", (group_id,)
                ).fetchone()
                if g_row:
                    gd = dict(g_row)
                    if _lua_already_incr:
                        token_delta = tokens - _reserved_tokens
                        cost_delta = cost - _reserved_cost
                        if token_delta != 0 or cost_delta != 0:
                            gdaily = max(0, int(gd.get("daily_tokens_used", "0")) + token_delta)
                            gmonthly = max(0.0, round(float(gd.get("monthly_cost_used", "0.0")) + cost_delta, 4))
                            tx.execute(
                                """UPDATE groups SET daily_tokens_used=?, monthly_cost_used=?
                                   WHERE group_id=?""",
                                (gdaily, gmonthly, group_id),
                            )
                    else:
                        grpm_ws = int(gd.get("rpm_window_start", 0))
                        grpm_wc = int(gd.get("rpm_window_count", 0)) + 1
                        if now_unix - grpm_ws >= 60:
                            grpm_ws = now_unix
                            grpm_wc = 1
                        gtpm_ws = int(gd.get("tpm_window_start", 0))
                        gtpm_wc = int(gd.get("tpm_window_count", 0)) + tokens
                        if now_unix - gtpm_ws >= 60:
                            gtpm_ws = now_unix
                            gtpm_wc = tokens
                        gdaily = int(gd.get("daily_tokens_used", "0")) + tokens
                        gmonthly = round(float(gd.get("monthly_cost_used", "0.0")) + cost, 4)
                        tx.execute(
                            """UPDATE groups SET rpm_window_start=?, rpm_window_count=?,
                               tpm_window_start=?, tpm_window_count=?,
                               daily_tokens_used=?, monthly_cost_used=?
                               WHERE group_id=?""",
                            (grpm_ws, grpm_wc, gtpm_ws, gtpm_wc, gdaily, gmonthly, group_id),
                        )
                    gq = self._quota_period_rows("group", group_id, today, month)
                    gdaily_q = self._accumulate_quota(gq.get("daily"), tokens, cost, model, tokens_in, tokens_out)
                    gmonthly_q = self._accumulate_quota(gq.get("monthly"), tokens, cost, model, tokens_in, tokens_out)
                    self._upsert_quota_record("group", group_id, "daily", today, gdaily_q)
                    self._upsert_quota_record("group", group_id, "monthly", month, gmonthly_q)

    async def _check_duplicate_user_key(self, user_id: str) -> None:
        if self._duplicate_user_key(user_id):
            row = self.conn.fetchone(
                "SELECT key_id FROM api_keys WHERE user_id=? AND status='active'",
                (user_id,),
            )
            raise ValueError(f"用户 '{user_id}' 已存在活跃 Key: {row['key_id'] if row else 'unknown'}")

    async def _find_key_hashes_by_id(self, key_id: str) -> List[str]:
        return self._lookup_by_id(key_id)

    async def migrate_groups(self, group_store) -> int:
        """Assign groupless keys to the default group."""
        default_id = await group_store.ensure_default_group()
        migrated = 0
        rows = self.conn.fetchall(
            "SELECT key_hash, cache_scope FROM api_keys WHERE group_id='' OR group_id IS NULL"
        )
        for r in rows:
            kh = r["key_hash"]
            cs = r["cache_scope"] or "group"
            with self.conn.transaction() as tx:
                tx.execute(
                    "UPDATE api_keys SET group_id=? WHERE key_hash=?",
                    (default_id, kh),
                )
                tx.execute(
                    "INSERT OR IGNORE INTO group_members (group_id, key_hash) VALUES (?, ?)",
                    (default_id, kh),
                )
            migrated += 1
        if migrated:
            logger.info("迁移 %d 个无组 Key 到默认组 %s", migrated, default_id)
        return migrated

    # ── Pub/Sub stub (no-op without Redis) ─────────────────────────

    async def publish(self, channel: str, message: dict) -> None:
        """Stub: publish pub/sub event. No-op if Redis not configured."""
        # If redis_mgr is available, forward the message
        try:
            from aigateway_api.app_state import get_state
            s = get_state()
            redis_mgr = getattr(s, "redis_manager", None)
            if redis_mgr and redis_mgr.redis:
                await redis_mgr.redis.publish(f"aigateway:{channel}", json.dumps(message))
        except Exception as exc:
            logger.debug("Pub/Sub publish failed (non-critical): %s", exc)

    # ── GroupStore-compatible API ─────────────────────────────────

    async def create_group(self, name: str,
                           quotas: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not name or not name.strip():
            raise ValueError("group name is required")
        name = name.strip()

        slug = _slugify(name)
        group_id = f"grp-{slug}" if slug else "grp-group"
        suffix = 2
        while self.conn.fetchone(
            "SELECT group_id FROM groups WHERE group_id=?", (group_id,)
        ):
            group_id = f"grp-{slug or 'group'}-{suffix}"
            suffix += 1

        q = quotas or {}
        now_iso = _now_iso()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month = datetime.now(timezone.utc).strftime("%Y-%m")

        with self.conn.transaction() as tx:
            # Check name uniqueness
            nrow = tx.execute(
                "SELECT group_id FROM groups WHERE name=?", (name,)
            ).fetchone()
            if nrow:
                raise ValueError(f"group '{name}' already exists")

            tx.execute(
                """INSERT INTO groups
                   (group_id, name, status, created_at, updated_at,
                    daily_tokens_limit, daily_tokens_used,
                    monthly_cost_limit, monthly_cost_used,
                    rate_limit_rpm, rate_limit_tpm,
                    rpm_window_start, rpm_window_count,
                    tpm_window_start, tpm_window_count)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    group_id, name, "active", now_iso, now_iso,
                    q.get("daily_tokens", self.DEFAULT_DAILY_TOKENS), 0,
                    q.get("monthly_cost", self.DEFAULT_MONTHLY_COST), 0.0,
                    q.get("rate_limit_rpm", self.DEFAULT_RATE_LIMIT_RPM),
                    q.get("rate_limit_tpm", self.DEFAULT_RATE_LIMIT_TPM),
                    _now_unix(), 0, _now_unix(), 0,
                ),
            )
            qb = _quota_base()
            tx.execute(
                """INSERT INTO quota_records
                   (entity_type, entity_id, period_type, period_value,
                    tokens_in, tokens_out, cost_usd, request_count, model_usage)
                   VALUES ('group', ?, 'daily', ?, ?, ?, ?, ?, ?)""",
                (group_id, today, qb["tokens_in"], qb["tokens_out"],
                 qb["cost_usd"], qb["request_count"], qb["model_usage"]),
            )
            tx.execute(
                """INSERT INTO quota_records
                   (entity_type, entity_id, period_type, period_value,
                    tokens_in, tokens_out, cost_usd, request_count, model_usage)
                   VALUES ('group', ?, 'monthly', ?, ?, ?, ?, ?, ?)""",
                (group_id, month, qb["tokens_in"], qb["tokens_out"],
                 qb["cost_usd"], qb["request_count"], qb["model_usage"]),
            )

        await self.publish("groups:sync", {
            "event_type": "group_created",
            "group_id": group_id,
            "name": name,
            "timestamp": now_iso,
        })
        logger.info("Group 创建: group_id=%s name=%s", group_id, name)
        return {"group_id": group_id, "name": name}

    async def get_group(self, group_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.fetchone(
            "SELECT * FROM groups WHERE group_id=?", (group_id,)
        )
        return dict(row) if row else None

    async def list_groups(self) -> List[Dict[str, Any]]:
        rows = self.conn.fetchall("SELECT * FROM groups")
        out: list[dict] = []
        for r in rows:
            g = dict(r)
            g["group_id"] = r["group_id"]
            g["member_count"] = await self.get_member_count(r["group_id"])
            for num_field in ("daily_tokens_limit", "daily_tokens_used",
                              "rate_limit_rpm", "rate_limit_tpm"):
                if num_field in g:
                    g[num_field] = int(g[num_field])
            for float_field in ("monthly_cost_limit", "monthly_cost_used"):
                if float_field in g:
                    g[float_field] = float(g[float_field])
            out.append(g)
        return out

    async def update_group(
        self,
        group_id: str,
        quotas: Optional[Dict[str, Any]] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        row = self.conn.fetchone(
            "SELECT * FROM groups WHERE group_id=?", (group_id,)
        )
        if not row:
            raise ValueError(f"group {group_id} not found")
        data = dict(row)
        if quotas:
            if "daily_tokens" in quotas:
                data["daily_tokens_limit"] = str(quotas["daily_tokens"])
            if "monthly_cost" in quotas:
                data["monthly_cost_limit"] = str(quotas["monthly_cost"])
            if "rate_limit_rpm" in quotas:
                data["rate_limit_rpm"] = str(quotas["rate_limit_rpm"])
            if "rate_limit_tpm" in quotas:
                data["rate_limit_tpm"] = str(quotas["rate_limit_tpm"])
        if status:
            data["status"] = status
        data["updated_at"] = _now_iso()

        self.conn.execute(
            """UPDATE groups SET name=?, status=?, daily_tokens_limit=?,
               daily_tokens_used=?, monthly_cost_limit=?, monthly_cost_used=?,
               rate_limit_rpm=?, rate_limit_tpm=?,
               rpm_window_start=?, rpm_window_count=?,
               tpm_window_start=?, tpm_window_count=?, updated_at=?
               WHERE group_id=?""",
            (
                data["name"], data["status"],
                data["daily_tokens_limit"], data["daily_tokens_used"],
                data["monthly_cost_limit"], data["monthly_cost_used"],
                data["rate_limit_rpm"], data["rate_limit_tpm"],
                data["rpm_window_start"], data["rpm_window_count"],
                data["tpm_window_start"], data["tpm_window_count"],
                data["updated_at"], group_id,
            ),
        )
        self.conn.commit()

        await self.publish("groups:sync", {
            "event_type": "group_updated",
            "group_id": group_id,
            "timestamp": _now_iso(),
        })
        return data

    async def delete_group(self, group_id: str) -> bool:
        if group_id == self.DEFAULT_GROUP_ID:
            raise ValueError("default group cannot be deleted")
        row = self.conn.fetchone(
            "SELECT * FROM groups WHERE group_id=?", (group_id,)
        )
        if not row:
            return False
        members = await self._get_members(group_id)
        if members:
            raise ValueError(f"group {group_id} still has {len(members)} members; reassign first")
        name = row["name"]

        with self.conn.transaction() as tx:
            tx.execute("DELETE FROM groups WHERE group_id=?", (group_id,))
            tx.execute("DELETE FROM quota_records WHERE entity_type='group' AND entity_id=?", (group_id,))
            tx.execute("DELETE FROM group_members WHERE group_id=?", (group_id,))

        await self.publish("groups:sync", {
            "event_type": "group_deleted",
            "group_id": group_id,
            "timestamp": _now_iso(),
        })
        return True

    async def add_member(self, group_id: str, key_hash: str) -> None:
        with self.conn.transaction() as tx:
            tx.execute(
                "INSERT OR IGNORE INTO group_members (group_id, key_hash) VALUES (?, ?)",
                (group_id, key_hash),
            )

    async def remove_member(self, group_id: str, key_hash: str) -> None:
        self.conn.execute(
            "DELETE FROM group_members WHERE group_id=? AND key_hash=?",
            (group_id, key_hash),
        )
        self.conn.commit()

    async def _get_members(self, group_id: str) -> List[str]:
        rows = self.conn.fetchall(
            "SELECT key_hash FROM group_members WHERE group_id=?", (group_id,)
        )
        return sorted(r["key_hash"] for r in rows)

    async def get_member_count(self, group_id: str) -> int:
        return len(await self._get_members(group_id))

    async def get_group_detail(self, group_id: str) -> Optional[Dict[str, Any]]:
        data = await self.get_group(group_id)
        if not data:
            return None
        data["group_id"] = group_id
        data["members"] = await self._get_members(group_id)
        data["member_count"] = len(data["members"])
        return data

    async def ensure_default_group(self) -> str:
        row = self.conn.fetchone(
            "SELECT group_id FROM groups WHERE name=?",
            (self.DEFAULT_GROUP_NAME,),
        )
        if row:
            return row["group_id"]
        try:
            g = await self.create_group(self.DEFAULT_GROUP_NAME, {})
        except ValueError:
            return self.DEFAULT_GROUP_ID
        return g["group_id"]

    async def assign_key_to_group(self, key_hash: str, new_group_id: str) -> None:
        if new_group_id == self.DEFAULT_GROUP_ID:
            raise ValueError("cannot assign to default group via this method")

        key_row = self._api_key_row(key_hash)
        if not key_row:
            raise ValueError(f"key {key_hash} not found")
        key_data = self._row_to_dict(key_row)

        old_group_id = key_data.get("group_id") or ""
        if old_group_id == new_group_id:
            return

        cache_scope = key_data.get("cache_scope", "group")

        new_row = self.conn.fetchone(
            "SELECT * FROM groups WHERE group_id=?", (new_group_id,)
        )
        if not new_row:
            raise ValueError(f"target group {new_group_id} not found")
        new_data = dict(new_row)

        moved_daily = int(key_data.get("daily_tokens_used", "0"))
        moved_monthly = float(key_data.get("monthly_cost_used", "0.0"))

        with self.conn.transaction() as tx:
            # Old group adjustments
            if old_group_id:
                old_row = tx.execute(
                    "SELECT * FROM groups WHERE group_id=?", (old_group_id,)
                ).fetchone()
                if old_row:
                    od = dict(old_row)
                    od["daily_tokens_used"] = str(
                        max(0, int(od.get("daily_tokens_used", "0")) - moved_daily)
                    )
                    od["monthly_cost_used"] = str(round(
                        max(0.0, float(od.get("monthly_cost_used", "0.0")) - moved_monthly), 4
                    ))
                    tx.execute(
                        """UPDATE groups SET daily_tokens_used=?, monthly_cost_used=?
                           WHERE group_id=?""",
                        (od["daily_tokens_used"], od["monthly_cost_used"], old_group_id),
                    )

            # New group adjustments
            new_data["daily_tokens_used"] = str(
                int(new_data.get("daily_tokens_used", "0")) + moved_daily
            )
            new_data["monthly_cost_used"] = str(round(
                float(new_data.get("monthly_cost_used", "0.0")) + moved_monthly, 4
            ))
            tx.execute(
                """UPDATE groups SET daily_tokens_used=?, monthly_cost_used=?
                   WHERE group_id=?""",
                (new_data["daily_tokens_used"], new_data["monthly_cost_used"], new_group_id),
            )

            # Key record
            key_data["group_id"] = new_group_id
            if "cache_scope" not in key_data or not key_data["cache_scope"]:
                key_data["cache_scope"] = cache_scope
            tx.execute(
                "UPDATE api_keys SET group_id=?, cache_scope=? WHERE key_hash=?",
                (new_group_id, key_data["cache_scope"], key_hash),
            )

            # Member sets
            if old_group_id:
                tx.execute(
                    "DELETE FROM group_members WHERE group_id=? AND key_hash=?",
                    (old_group_id, key_hash),
                )
            tx.execute(
                "INSERT OR IGNORE INTO group_members (group_id, key_hash) VALUES (?, ?)",
                (new_group_id, key_hash),
            )

            # Quota period transfers
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            month = datetime.now(timezone.utc).strftime("%Y-%m")
            if old_group_id:
                oq = self._quota_period_rows("group", old_group_id, today, month)
                for pt, pv in [("daily", today), ("monthly", month)]:
                    pq = oq.get(pt) or _quota_base()
                    if pt == "daily":
                        pq["tokens_in"] = str(max(0, int(pq["tokens_in"]) - moved_daily))
                        pq["tokens_out"] = str(max(0, int(pq["tokens_out"]) - moved_daily))
                    else:
                        pq["cost_usd"] = str(round(max(0.0, float(pq["cost_usd"]) - moved_monthly), 4))
                    self._upsert_quota_record("group", old_group_id, pt, pv, pq)
            nq = self._quota_period_rows("group", new_group_id, today, month)
            for pt, pv in [("daily", today), ("monthly", month)]:
                pq = nq.get(pt) or _quota_base()
                if pt == "daily":
                    pq["tokens_in"] = str(int(pq["tokens_in"]) + moved_daily)
                    pq["tokens_out"] = str(int(pq["tokens_out"]) + moved_daily)
                else:
                    pq["cost_usd"] = str(round(float(pq["cost_usd"]) + moved_monthly, 4))
                self._upsert_quota_record("group", new_group_id, pt, pv, pq)

        await self.publish("groups:sync", {
            "event_type": "key_assigned",
            "key_hash": key_hash,
            "from_group": old_group_id,
            "to_group": new_group_id,
            "timestamp": _now_iso(),
        })
        logger.info("Key %s assigned to group %s (was %s)", key_hash, new_group_id, old_group_id)

    # ── Legacy KeyStore/GroupStore compatibility methods ──────────

    async def get_api_key(self, key_hash: str) -> Optional[Dict[str, Any]]:
        row = self._api_key_row(key_hash)
        return self._row_to_dict(row)

    async def set_api_key(self, key_hash: str, data: Dict[str, Any]) -> None:
        # 运行时计数器列只能由 check_quota / increment_usage 修改 —— 这里若传进来
        # 会用旧快照覆盖并发的计数写入，静默回滚配额。调用方应只传要改的限制/元数据列。
        _RUNTIME_COUNTER_COLS = {
            "daily_tokens_used", "monthly_cost_used",
            "rpm_window_start", "rpm_window_count",
            "tpm_window_start", "tpm_window_count",
            "last_used_at",
        }
        # Build dynamic UPDATE
        fields = []
        values = []
        for k, v in data.items():
            if k in ("key_hash", "key_id", "created_at") or k in _RUNTIME_COUNTER_COLS:
                continue
            fields.append(f"{k}=?")
            # Convert bool to int
            if isinstance(v, bool):
                v = int(v)
            values.append(str(v) if v is not None else v)
        values.append(key_hash)
        if fields:
            self.conn.execute(
                f"UPDATE api_keys SET {', '.join(fields)} WHERE key_hash=?",
                values,
            )
            self.conn.commit()

    async def delete_api_key(self, key_hash: str, key_prefix: str) -> int:
        self.conn.execute("DELETE FROM quota_records WHERE entity_type='key' AND entity_id=?", (key_hash,))
        self.conn.execute("DELETE FROM group_members WHERE key_hash=?", (key_hash,))
        cur = self.conn.execute("DELETE FROM api_keys WHERE key_hash=?", (key_hash,))
        self.conn.commit()
        return cur.rowcount

    async def set_key_lookup(self, key_prefix: str, key_hash: str) -> None:
        # Not needed for SQLite (key_prefix stored in api_keys table)
        pass

    async def get_key_lookup(self, key_prefix: str) -> Optional[str]:
        row = self.conn.fetchone(
            "SELECT key_hash FROM api_keys WHERE key_prefix=?", (key_prefix,)
        )
        return row["key_hash"] if row else None

    async def set_group_lookup(self, name: str, group_id: str) -> None:
        # Not needed for SQLite (name stored in groups table)
        pass

    async def get_group_lookup(self, name: str) -> Optional[str]:
        row = self.conn.fetchone(
            "SELECT group_id FROM groups WHERE name=?", (name,)
        )
        return row["group_id"] if row else None

    async def delete_group_lookup(self, name: str) -> None:
        pass  # name is in groups table

    async def set_quota(self, entity_id: str, period: str,
                        data: Dict[str, Any]) -> None:
        parts = period.split(":", 1)
        if len(parts) != 2:
            return
        period_type, period_value = parts
        self._upsert_quota_record("key" if ":" in entity_id else "group",
                                  entity_id, period_type, period_value, data)

    async def get_quota(self, entity_id: str, period: str) -> Optional[Dict[str, Any]]:
        parts = period.split(":", 1)
        if len(parts) != 2:
            return None
        period_type, period_value = parts
        row = self.conn.fetchone(
            "SELECT * FROM quota_records WHERE entity_id=? AND period_type=? AND period_value=?",
            (entity_id, period_type, period_value),
        )
        return dict(row) if row else None

    async def pipe_batch(self, fn) -> list:
        """Execute a batch of operations within a transaction.

        Accepts a callable that receives a list to append SQL commands to.
        Returns list of rowcounts.
        """
        results = []
        with self.conn.transaction() as tx:
            cmds = fn([])
            for cmd in cmds:
                if callable(cmd):
                    results.append(cmd())
                elif isinstance(cmd, tuple):
                    sql, params = cmd
                    results.append(tx.execute(sql, params).rowcount)
        return results

    # ── Cost ledger ───────────────────────────────────────────────

    async def record_request_cost(
        self,
        *,
        trace_id: str = "",
        user_id: str = "",
        group_id: str = "",
        model: str = "",
        provider: str = "",
        pipeline_kind: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        tokens_total: int = 0,
        cost_usd: float = 0.0,
        cached: bool = False,
        stream: bool = False,
        status: str = "ok",
    ) -> None:
        """Append a per-request cost record to the ledger.

        Best-effort: failures are logged and swallowed so a ledger write
        can never break the response path. Survives container rebuilds
        because the DB file lives on a mounted volume.
        """
        try:
            self.conn.execute(
                """INSERT INTO request_cost_ledger
                   (trace_id, ts, ts_unix, user_id, group_id, model, provider,
                    pipeline_kind, tokens_in, tokens_out, tokens_total, cost_usd,
                    cached, stream, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trace_id or "", _now_iso(), _now_unix(),
                    user_id or "", group_id or "", model or "", provider or "",
                    pipeline_kind or "",
                    int(tokens_in or 0), int(tokens_out or 0), int(tokens_total or 0),
                    float(cost_usd or 0.0),
                    1 if cached else 0, 1 if stream else 0, status or "ok",
                ),
            )
            self.conn.commit()
        except Exception as exc:
            logger.warning("record_request_cost 失败: %s", exc)

    async def prune_ledger(self, keep_days: int = 90) -> int:
        """Delete ledger rows older than keep_days. Returns deleted count."""
        try:
            cutoff = _now_unix() - keep_days * 86400
            cur = self.conn.execute(
                "DELETE FROM request_cost_ledger WHERE ts_unix < ?", (cutoff,)
            )
            self.conn.commit()
            deleted = cur.rowcount or 0
            if deleted:
                logger.info("清理成本账本: 删除 %d 条 %d 天前的记录", deleted, keep_days)
            return deleted
        except Exception as exc:
            logger.warning("prune_ledger 失败: %s", exc)
            return 0

    async def query_ledger(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        start_unix: Optional[int] = None,
        end_unix: Optional[int] = None,
        user_id: Optional[str] = None,
        group_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Query ledger rows, newest first."""
        where: list[str] = []
        params: list = []
        if start_unix is not None:
            where.append("ts_unix >= ?")
            params.append(int(start_unix))
        if end_unix is not None:
            where.append("ts_unix <= ?")
            params.append(int(end_unix))
        if user_id:
            where.append("user_id = ?")
            params.append(user_id)
        if group_id:
            where.append("group_id = ?")
            params.append(group_id)
        if model:
            where.append("model = ?")
            params.append(model)
        sql = "SELECT * FROM request_cost_ledger"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts_unix DESC LIMIT ? OFFSET ?"
        params.append(int(limit))
        params.append(int(offset))
        try:
            rows = self.conn.fetchall(sql, tuple(params))
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("query_ledger 失败: %s", exc)
            return []

    async def ledger_summary(
        self,
        *,
        start_unix: Optional[int] = None,
        end_unix: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Aggregate cost/token stats grouped by model/user/group/day."""
        where: list[str] = []
        params: list = []
        if start_unix is not None:
            where.append("ts_unix >= ?")
            params.append(int(start_unix))
        if end_unix is not None:
            where.append("ts_unix <= ?")
            params.append(int(end_unix))
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        def _agg(group_cols: str) -> str:
            return (
                f"SELECT {group_cols} AS k, COUNT(*) AS requests, "
                "COALESCE(SUM(tokens_in),0) AS tokens_in, "
                "COALESCE(SUM(tokens_out),0) AS tokens_out, "
                "COALESCE(SUM(tokens_total),0) AS tokens_total, "
                "COALESCE(SUM(cost_usd),0) AS cost_usd, "
                "COALESCE(SUM(CASE WHEN cached=1 THEN 1 ELSE 0 END),0) AS cache_hits "
                f"FROM request_cost_ledger{clause} "
                f"GROUP BY {group_cols} ORDER BY cost_usd DESC"
            )

        try:
            total_row = self.conn.fetchone(
                "SELECT COUNT(*) AS requests, "
                "COALESCE(SUM(tokens_in),0) AS tokens_in, "
                "COALESCE(SUM(tokens_out),0) AS tokens_out, "
                "COALESCE(SUM(tokens_total),0) AS tokens_total, "
                "COALESCE(SUM(cost_usd),0) AS cost_usd, "
                "COALESCE(SUM(CASE WHEN cached=1 THEN 1 ELSE 0 END),0) AS cache_hits "
                f"FROM request_cost_ledger{clause}",
                tuple(params),
            )
            by_model = [dict(r) for r in self.conn.fetchall(_agg("model"), tuple(params))]
            by_user = [dict(r) for r in self.conn.fetchall(_agg("user_id"), tuple(params))]
            by_group = [dict(r) for r in self.conn.fetchall(_agg("group_id"), tuple(params))]
            day_rows = self.conn.fetchall(
                "SELECT substr(ts,1,10) AS k, COUNT(*) AS requests, "
                "COALESCE(SUM(tokens_total),0) AS tokens_total, "
                "COALESCE(SUM(cost_usd),0) AS cost_usd "
                f"FROM request_cost_ledger{clause} "
                "GROUP BY substr(ts,1,10) ORDER BY k ASC",
                tuple(params),
            )
            return {
                "total": dict(total_row) if total_row else {},
                "by_model": by_model,
                "by_user": by_user,
                "by_group": by_group,
                "by_day": [dict(r) for r in day_rows],
            }
        except Exception as exc:
            logger.warning("ledger_summary 失败: %s", exc)
            return {"total": {}, "by_model": [], "by_user": [], "by_group": [], "by_day": []}

    # ── Cleanup ───────────────────────────────────────────────────

    def close(self):
        self.conn.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
