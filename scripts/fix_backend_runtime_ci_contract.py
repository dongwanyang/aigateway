#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"expected block not found in {path}: {old[:160]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "aigateway-api/src/aigateway_api/admin_routes.py",
    '''    logger.info("API Key 配额已更新: key_id=%s, fields=%s", key_id, list(updated_fields.keys()))

    return {
        "data": {
            "id": key_id,
            "user_id": updated_data.get("user_id", ""),
            "quotas": {
                "daily_tokens_limit": int(updated_data.get("daily_tokens_limit", _get_auth_defaults()["daily_tokens"])),
                "monthly_cost_limit": float(updated_data.get("monthly_cost_limit", _get_auth_defaults()["monthly_cost"])),
                "rate_limit_rpm": int(updated_data.get("rate_limit_rpm", _get_auth_defaults()["rate_limit_rpm"])),
                "rate_limit_tpm": int(updated_data.get("rate_limit_tpm", _get_auth_defaults()["rate_limit_tpm"])),
            },
        },
''',
    '''    logger.info("API Key 配额已更新: key_id=%s, fields=%s", key_id, list(updated_fields.keys()))
    defaults = _get_auth_defaults()

    return {
        "data": {
            "id": key_id,
            "user_id": updated_data.get("user_id", ""),
            "quotas": {
                "daily_tokens_limit": int(updated_data.get("daily_tokens_limit", defaults["daily_tokens"])),
                "monthly_cost_limit": float(updated_data.get("monthly_cost_limit", defaults["monthly_cost"])),
                "rate_limit_rpm": int(updated_data.get("rate_limit_rpm", defaults["rate_limit_rpm"])),
                "rate_limit_tpm": int(updated_data.get("rate_limit_tpm", defaults["rate_limit_tpm"])),
            },
        },
''',
)

replace_once(
    "tests/unit/api/test_main_configuration.py",
    '''    assert gateway.status_code == 500
    assert gateway.json()["error"]["code"] == "internal_error"
    assert "[REDACTED]" in gateway.json()["error"]["detail"]
    assert "/home/service" not in gateway.text
    assert "sk-" + "a" * 24 not in gateway.text
''',
    '''    assert gateway.status_code == 500
    assert gateway.json()["error"] == {
        "code": "internal_error",
        "message": "Internal Server Error",
    }
    assert "detail" not in gateway.json()["error"]
    assert "/home/service" not in gateway.text
    assert "sk-" + "a" * 24 not in gateway.text
''',
)
replace_once(
    "tests/unit/api/test_main_configuration.py",
    '''    assert unknown.json()["error"]["message"] == "Internal Server Error"
    assert "password=secret" not in unknown.text
''',
    '''    assert unknown.json()["error"] == {
        "code": "internal_error",
        "message": "Internal Server Error",
    }
    assert "detail" not in unknown.json()["error"]
    assert "password=secret" not in unknown.text
''',
)
