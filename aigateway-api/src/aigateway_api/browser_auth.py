"""Control-panel account and opaque browser-session storage.

API keys remain machine credentials. Browser cookies contain only random session
secrets whose hashes are persisted in the existing SQLite auth database.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _now_unix() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _password_hash(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    iterations = int(os.environ.get("AI_GATEWAY_PASSWORD_PBKDF2_ITERATIONS", "600000"))
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_iterations, raw_salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(raw_iterations)
        salt = bytes.fromhex(raw_salt)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual.hex(), expected)
    except (TypeError, ValueError):
        return False


class BrowserAuthStore:
    """SQLite-backed administrator account and revocable browser sessions."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS admin_users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    requires_password_change INTEGER NOT NULL DEFAULT 0,
                    password_changed_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS browser_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    absolute_expires_at INTEGER NOT NULL,
                    revoked_at INTEGER,
                    ip_address TEXT,
                    user_agent TEXT,
                    FOREIGN KEY(user_id) REFERENCES admin_users(user_id)
                );

                CREATE INDEX IF NOT EXISTS idx_browser_sessions_user
                    ON browser_sessions(user_id);
                CREATE INDEX IF NOT EXISTS idx_browser_sessions_expiry
                    ON browser_sessions(expires_at);
                """
            )

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM admin_users WHERE username=?", (username,)
            ).fetchone()
        return dict(row) if row else None

    def has_users(self) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM admin_users LIMIT 1").fetchone()
        return row is not None

    def provision_admin(self, username: str, temporary_password: str) -> Dict[str, Any]:
        now = _now_unix()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO admin_users
                   (user_id, username, password_hash, status,
                    requires_password_change, password_changed_at, created_at, updated_at)
                   VALUES ('admin', ?, ?, 'active', 1, ?, ?, ?)""",
                (username, _password_hash(temporary_password), now, now, now),
            )
        return self.get_user(username) or {}

    def verify_credentials(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        user = self.get_user(username)
        if not user or user.get("status") != "active":
            # Keep a comparable slow-hash cost for unknown users.
            _password_hash(password, salt=b"\0" * 16)
            return None
        if not _verify_password(password, str(user.get("password_hash", ""))):
            return None
        return user

    def create_session(
        self,
        user_id: str,
        *,
        ttl_seconds: int,
        absolute_ttl_seconds: int,
        ip_address: str = "",
        user_agent: str = "",
    ) -> str:
        token = secrets.token_urlsafe(32)
        now = _now_unix()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO browser_sessions
                   (token_hash, user_id, created_at, last_seen_at, expires_at,
                    absolute_expires_at, ip_address, user_agent)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _token_hash(token), user_id, now, now, now + ttl_seconds,
                    now + absolute_ttl_seconds, ip_address[:128], user_agent[:512],
                ),
            )
        return token

    def validate_session(self, token: str, *, idle_ttl_seconds: int) -> Optional[Dict[str, Any]]:
        now = _now_unix()
        token_digest = _token_hash(token)
        with self._connect() as conn:
            row = conn.execute(
                """SELECT s.*, u.username, u.status, u.requires_password_change
                   FROM browser_sessions s
                   JOIN admin_users u ON u.user_id=s.user_id
                   WHERE s.token_hash=? AND s.revoked_at IS NULL""",
                (token_digest,),
            ).fetchone()
            if not row:
                return None
            data = dict(row)
            if (
                data.get("status") != "active"
                or now >= int(data["expires_at"])
                or now >= int(data["absolute_expires_at"])
            ):
                conn.execute(
                    "UPDATE browser_sessions SET revoked_at=? WHERE token_hash=?",
                    (now, token_digest),
                )
                return None
            next_expiry = min(now + idle_ttl_seconds, int(data["absolute_expires_at"]))
            conn.execute(
                "UPDATE browser_sessions SET last_seen_at=?, expires_at=? WHERE token_hash=?",
                (now, next_expiry, token_digest),
            )
            data["expires_at"] = next_expiry
        return data

    def revoke_session(self, token: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE browser_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                (_now_unix(), _token_hash(token)),
            )

    def change_password(self, user_id: str, new_password: str) -> None:
        now = _now_unix()
        with self._connect() as conn:
            conn.execute(
                """UPDATE admin_users
                   SET password_hash=?, requires_password_change=0,
                       password_changed_at=?, updated_at=?
                   WHERE user_id=?""",
                (_password_hash(new_password), now, now, user_id),
            )
            conn.execute(
                "UPDATE browser_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (now, user_id),
            )


def get_browser_auth_store(request: Any) -> BrowserAuthStore:
    store = getattr(request.app.state, "browser_auth_store", None)
    if store is not None:
        return store
    key_store = getattr(request.app.state, "key_store", None)
    db_path = getattr(key_store, "db_path", None) or os.environ.get(
        "AI_GATEWAY_AUTH_DB_PATH", "data/auth.db"
    )
    store = BrowserAuthStore(db_path)
    request.app.state.browser_auth_store = store
    return store
