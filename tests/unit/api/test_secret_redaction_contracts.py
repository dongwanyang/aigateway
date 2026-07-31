from __future__ import annotations

from aigateway_api.config_security import (
    MASKED_SECRET,
    redact_config,
    restore_masked_values,
)


def test_sensitive_environment_expression_default_is_not_exposed() -> None:
    safe = redact_config(
        {
            "providers": {
                "openai": {
                    "api_key": "${OPENAI_API_KEY:-literal-default-secret}"
                }
            }
        }
    )

    assert safe["providers"]["openai"]["api_key"] == MASKED_SECRET


def test_stable_id_allows_name_change_without_secret_misassignment() -> None:
    current = {
        "items": [
            {
                "id": "stable-one",
                "name": "old-name",
                "api_key": "secret-one",
            },
            {
                "id": "stable-two",
                "name": "other-name",
                "api_key": "secret-two",
            },
        ]
    }
    candidate = redact_config(current)
    candidate["items"][0]["name"] = "renamed"

    restored = restore_masked_values(candidate, current)

    assert restored["items"][0]["name"] == "renamed"
    assert restored["items"][0]["api_key"] == "secret-one"
