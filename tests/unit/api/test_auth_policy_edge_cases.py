from __future__ import annotations

from types import SimpleNamespace

import pytest
from aigateway_api import auth_routes, browser_auth


def test_session_policy_rejects_invalid_environment_values(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_SESSION_TTL_SECONDS", "not-an-int")
    with pytest.raises(RuntimeError, match="invalid AI_GATEWAY_SESSION_TTL_SECONDS"):
        auth_routes._session_ttl()

    monkeypatch.setenv("AI_GATEWAY_SESSION_TTL_SECONDS", "0")
    with pytest.raises(RuntimeError, match="invalid AI_GATEWAY_SESSION_TTL_SECONDS"):
        auth_routes._session_ttl()


def test_session_policy_does_not_hide_invalid_yaml(monkeypatch):
    monkeypatch.delenv("AI_GATEWAY_SESSION_TTL_SECONDS", raising=False)

    def invalid_number(path: str, number_type):
        raise RuntimeError(f"runtime_config_invalid:{path}")

    monkeypatch.setattr(auth_routes, "configured_number", invalid_number)
    with pytest.raises(RuntimeError, match="runtime_config_invalid"):
        auth_routes._session_ttl()


def test_admin_username_does_not_hide_invalid_yaml(monkeypatch):
    monkeypatch.delenv("AI_GATEWAY_ADMIN_USERNAME", raising=False)

    def invalid_text(path: str):
        raise RuntimeError(f"runtime_config_invalid:{path}")

    monkeypatch.setattr(auth_routes, "configured_text", invalid_text)
    with pytest.raises(RuntimeError, match="runtime_config_invalid"):
        auth_routes._admin_username()


def test_proxy_headers_require_explicit_trust(monkeypatch):
    request = SimpleNamespace(
        headers={"x-forwarded-proto": "https", "x-forwarded-for": "203.0.113.8"},
        client=SimpleNamespace(host="10.0.0.5"),
        url=SimpleNamespace(scheme="http"),
    )
    monkeypatch.delenv("AI_GATEWAY_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("AI_GATEWAY_TRUSTED_PROXY_IPS", raising=False)
    assert auth_routes._is_https(request) is False
    assert auth_routes._client_ip(request) == "10.0.0.5"

    monkeypatch.setenv("AI_GATEWAY_TRUSTED_PROXY_IPS", "10.0.0.5")
    assert auth_routes._is_https(request) is True
    assert auth_routes._client_ip(request) == "203.0.113.8"


def test_admin_api_key_matching_is_fail_closed(monkeypatch):
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    assert auth_routes._matches_admin_api_key("candidate") is False

    monkeypatch.setenv("ADMIN_API_KEY", "secret-value")
    assert auth_routes._matches_admin_api_key("secret-value") is True
    assert auth_routes._matches_admin_api_key("other-value") is False


def test_browser_auth_optional_config_only_swallows_missing(monkeypatch):
    def missing_number(path: str, number_type):
        raise RuntimeError(f"runtime_config_missing:{path}")

    monkeypatch.setattr(browser_auth, "configured_number", missing_number)
    assert browser_auth._optional_config_number("auth.timeout", int, 30) == 30

    def invalid_number(path: str, number_type):
        raise RuntimeError(f"runtime_config_invalid:{path}")

    monkeypatch.setattr(browser_auth, "configured_number", invalid_number)
    with pytest.raises(RuntimeError, match="runtime_config_invalid"):
        browser_auth._optional_config_number("auth.timeout", int, 30)


def test_browser_auth_environment_numbers_are_positive(monkeypatch):
    name = "AI_GATEWAY_AUTH_DATABASE_TIMEOUT_SECONDS"
    monkeypatch.delenv(name, raising=False)
    assert browser_auth._positive_environment_number(name, float) is None

    monkeypatch.setenv(name, "invalid")
    with pytest.raises(RuntimeError, match=f"invalid {name}"):
        browser_auth._positive_environment_number(name, float)

    monkeypatch.setenv(name, "-1")
    with pytest.raises(RuntimeError, match=f"invalid {name}"):
        browser_auth._positive_environment_number(name, float)


def test_password_verification_rejects_invalid_encodings():
    assert browser_auth._verify_password("password", "sha256$1$00$00") is False
    assert browser_auth._verify_password("password", "malformed") is False


def test_admin_user_id_precedence_and_fallback(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_ADMIN_USER_ID", "operator-7")
    assert browser_auth._admin_user_id("admin") == "operator-7"

    monkeypatch.delenv("AI_GATEWAY_ADMIN_USER_ID", raising=False)
    monkeypatch.setattr(browser_auth, "_optional_config_text", lambda path: "")
    assert browser_auth._admin_user_id("admin") == "admin"


def test_browser_auth_store_validates_path_and_timeout(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="auth database path is required"):
        browser_auth.BrowserAuthStore("  ")

    monkeypatch.delenv("AI_GATEWAY_AUTH_DATABASE_TIMEOUT_SECONDS", raising=False)
    with pytest.raises(ValueError, match="auth database timeout must be positive"):
        browser_auth.BrowserAuthStore(str(tmp_path / "auth.db"), timeout_seconds=0)
