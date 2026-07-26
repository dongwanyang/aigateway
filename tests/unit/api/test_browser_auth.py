from __future__ import annotations

import hashlib

from aigateway_api.browser_auth import BrowserAuthStore


def test_browser_session_is_opaque_and_revocable(tmp_path):
    store = BrowserAuthStore(str(tmp_path / "auth.db"))
    initial_password = "temporary-admin-password"
    user = store.provision_admin("admin", initial_password)

    token = store.create_session(
        user["user_id"],
        ttl_seconds=3600,
        absolute_ttl_seconds=7200,
        ip_address="127.0.0.1",
        user_agent="pytest",
    )

    assert token != initial_password
    assert store.validate_session(token, idle_ttl_seconds=3600) is not None

    with store._connect() as conn:
        row = conn.execute(
            "SELECT token_hash FROM browser_sessions WHERE user_id=?",
            (user["user_id"],),
        ).fetchone()
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in row["token_hash"]

    store.revoke_session(token)
    assert store.validate_session(token, idle_ttl_seconds=3600) is None


def test_password_change_revokes_old_sessions(tmp_path):
    store = BrowserAuthStore(str(tmp_path / "auth.db"))
    initial_password = "temporary-admin-password"
    new_password = "a-new-independent-password"
    user = store.provision_admin("admin", initial_password)
    token = store.create_session(
        user["user_id"], ttl_seconds=3600, absolute_ttl_seconds=7200
    )

    assert store.verify_credentials("admin", initial_password) is not None

    store.change_password(user["user_id"], new_password)

    assert store.verify_credentials("admin", initial_password) is None
    updated = store.verify_credentials("admin", new_password)
    assert updated is not None
    assert updated["requires_password_change"] == 0
    assert store.validate_session(token, idle_ttl_seconds=3600) is None
