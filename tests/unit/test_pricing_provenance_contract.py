from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import yaml


def _write_config(tmp_path, providers: dict) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({"providers": providers}, allow_unicode=True),
        encoding="utf-8",
    )
    return str(path)


def test_unpriced_litellm_placeholder_is_not_used_for_runtime_ranking(
    monkeypatch,
):
    register_model = Mock()
    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(register_model=register_model),
    )

    from aigateway_core.route.bridge import LiteLLMBridge

    bridge = LiteLLMBridge({})
    bridge._register_model_pricing(
        {},
        "openai/unpriced-model",
        "https://provider.example/v1",
        "demo",
    )

    registration = register_model.call_args.args[0]["openai/unpriced-model"]
    assert registration["input_cost_per_token"] == 0.0
    assert registration["output_cost_per_token"] == 0.0
    assert "openai/unpriced-model" not in bridge._model_pricing
    assert "unpriced-model" not in bridge._model_pricing

    bridge._register_model_pricing(
        {
            "pricing": {
                "free-model": {"prompt": 0.0, "completion": 0.0},
            }
        },
        "openai/free-model",
        "https://provider.example/v1",
        "demo",
    )
    assert bridge._model_pricing["openai/free-model"] == {
        "prompt": 0.0,
        "completion": 0.0,
    }
    assert bridge._model_pricing["free-model"] == {
        "prompt": 0.0,
        "completion": 0.0,
    }


def test_default_pricing_is_scoped_to_its_model_group(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "AI_GATEWAY_CONFIG_PATH",
        _write_config(
            tmp_path,
            {
                "demo": {
                    "model_grouper": [
                        {
                            "models": [{"name": "group-a-model"}],
                            "pricing": {
                                "$default": {
                                    "prompt": 0.01,
                                    "completion": 0.02,
                                }
                            },
                        },
                        {
                            "models": [{"name": "group-b-model"}],
                            "pricing": {},
                        },
                    ]
                }
            },
        ),
    )

    from aigateway_core.shared.runtime_values import configured_model_pricing

    assert configured_model_pricing("group-a-model") == {
        "prompt": 0.01,
        "completion": 0.02,
    }
    assert configured_model_pricing("group-b-model") is None
    assert configured_model_pricing("unknown-model") is None


def test_plain_numeric_cost_preserves_free_and_unpriced_quota_provenance(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(
        "AI_GATEWAY_CONFIG_PATH",
        _write_config(
            tmp_path,
            {
                "demo": {
                    "model_grouper": [
                        {
                            "models": [
                                {"name": "free-model"},
                                {"name": "paid-model"},
                                {"name": "unpriced-model"},
                            ],
                            "pricing": {
                                "free-model": {
                                    "prompt": 0.0,
                                    "completion": 0.0,
                                },
                                "paid-model": {
                                    "prompt": 0.01,
                                    "completion": 0.02,
                                },
                            },
                        }
                    ]
                }
            },
        ),
    )

    from aigateway_core.shared.auth.sqlite_store import SQLiteStore

    store = SQLiteStore(str(tmp_path / "auth.db"))
    try:
        expected = (
            ("free-model", 0.0, "free"),
            ("paid-model", 0.3, "priced"),
            ("unpriced-model", 0.0, "unpriced"),
        )
        for model, cost, status in expected:
            quota = store._accumulate_quota(
                None,
                tokens=15,
                cost=cost,
                model=model,
                tokens_in=10,
                tokens_out=5,
            )
            usage = json.loads(quota["model_usage"])[model]
            assert usage["pricing_status"] == status
            assert usage[f"{status}_requests"] == 1
    finally:
        store.close()
