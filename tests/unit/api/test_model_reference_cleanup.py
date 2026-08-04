from __future__ import annotations

import pytest

from aigateway_api.config_security import ConfigValidationError
from aigateway_api.security_routes import (
    _configured_model_names,
    _prune_removed_model_references,
)


def test_removed_model_references_are_pruned_without_touching_unknown_aliases() -> None:
    config = {
        "providers": {
            "primary": {
                "model_grouper": [
                    {
                        "models": [
                            {"name": "kept-model"},
                            {"name": "duplicate-model"},
                        ],
                        "fallback_models": [
                            "removed-model",
                            "kept-model",
                            "external-alias",
                        ],
                        "pricing": {
                            "removed-model": {"prompt": 1},
                            "kept-model": {"prompt": 2},
                        },
                    }
                ]
            },
            "secondary": {
                "model_grouper": [
                    {
                        "models": ["duplicate-model"],
                        "fallback_models": [],
                        "pricing": {},
                    }
                ]
            },
        },
        "task_routing": {
            "model_preferences": {
                "reasoning": [
                    "removed-model",
                    "kept-model",
                    "external-alias",
                ]
            }
        },
        "generation_optimization": {
            "model_router": {
                "model_capabilities": {
                    "removed-model": 50,
                    "kept-model": 80,
                    "external-alias": 10,
                },
                "model_modalities": {
                    "removed-model": ["llm"],
                    "kept-model": ["llm"],
                },
            }
        },
    }

    assert _configured_model_names(config) == {
        "kept-model",
        "duplicate-model",
    }
    _prune_removed_model_references(config, {"removed-model"})

    group = config["providers"]["primary"]["model_grouper"][0]
    assert group["fallback_models"] == ["kept-model", "external-alias"]
    assert set(group["pricing"]) == {"kept-model"}
    assert config["task_routing"]["model_preferences"]["reasoning"] == [
        "kept-model",
        "external-alias",
    ]
    assert config["generation_optimization"]["model_router"][
        "model_capabilities"
    ] == {
        "kept-model": 80,
        "external-alias": 10,
    }
    assert config["generation_optimization"]["model_router"][
        "model_modalities"
    ] == {"kept-model": ["llm"]}


def test_duplicate_model_name_is_not_considered_removed() -> None:
    before = {
        "providers": {
            "one": {"model_grouper": [{"models": [{"name": "shared"}]}]},
            "two": {"model_grouper": [{"models": [{"name": "shared"}]}]},
        }
    }
    after = {
        "providers": {
            "two": {"model_grouper": [{"models": [{"name": "shared"}]}]},
        },
        "task_routing": {"model_preferences": {"general": ["shared"]}},
    }

    removed = _configured_model_names(before) - _configured_model_names(after)
    assert removed == set()
    _prune_removed_model_references(after, removed)
    assert after["task_routing"]["model_preferences"]["general"] == ["shared"]


def test_scalar_model_references_require_explicit_replacement() -> None:
    removed = "removed-model"
    config = {
        "intent_classifier": {"model": removed},
        "generation_optimization": {
            "ai_director": {"rewrite_model": removed},
            "model_router": {"default_model": removed},
            "draft_workflow": {"draft_model": removed},
        },
        "media_optimization": {"image": {"caption_model": removed}},
        "plugins": [
            {
                "name": "conv_compressor",
                "config": {"summary_model": removed},
            }
        ],
    }

    with pytest.raises(ConfigValidationError) as caught:
        _prune_removed_model_references(config, {removed})

    message = "\n".join(str(issue["message"]) for issue in caught.value.issues)
    assert "intent_classifier.model" in message
    assert "generation_optimization.ai_director.rewrite_model" in message
    assert "generation_optimization.model_router.default_model" in message
    assert "generation_optimization.draft_workflow.draft_model" in message
    assert "media_optimization.image.caption_model" in message
    assert "plugins.0.config.summary_model" in message
    assert "select a replacement model first" in message
