"""Regression tests for configuration schema extraction."""
from __future__ import annotations

from pathlib import Path

import pytest

from aigateway_api.config_schema import parse_template_schema

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = REPO_ROOT / "config.yaml.template"


@pytest.fixture
def schema_items(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    monkeypatch.setenv("AI_GATEWAY_CONFIG_TEMPLATE_PATH", str(TEMPLATE_PATH))
    return parse_template_schema(str(REPO_ROOT / "config.yaml"))


def _by_path(items: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(item["path"]): item for item in items}


def test_provider_and_pricing_paths_are_wildcarded(
    schema_items: list[dict[str, object]],
) -> None:
    by_path = _by_path(schema_items)
    assert "providers.*.api_key" in by_path
    assert "providers.*.model_grouper[].models[].features" in by_path
    assert "providers.*.model_grouper[].pricing.*.prompt" in by_path
    assert "providers.*.model_grouper[].pricing.*.completion" in by_path
    assert not any(path.startswith("providers.agnes.") for path in by_path)
    assert not any(path.startswith("providers.deepseek.") for path in by_path)


def test_dynamic_leaf_descriptions_are_not_reduced_to_parent_text(
    schema_items: list[dict[str, object]],
) -> None:
    by_path = _by_path(schema_items)
    assert by_path["providers.*.model_grouper[].pricing.*.prompt"][
        "description"
    ] == "输入 token 单价"
    assert by_path["providers.*.model_grouper[].pricing.*.completion"][
        "description"
    ] == "输出 token 单价"
    assert "模型标识符" in str(
        by_path["providers.*.model_grouper[].models[].name"]["description"]
    )


def test_array_element_types_are_reported(
    schema_items: list[dict[str, object]],
) -> None:
    by_path = _by_path(schema_items)
    assert by_path["providers.*.model_grouper[].models[].features"][
        "value_type"
    ] == "string[]"
    assert by_path["providers.*.model_grouper[].fallback_models"][
        "value_type"
    ] == "string[]"
    assert by_path["cache.key_buckets.max_tokens"]["value_type"] == "integer[]"


def test_missing_inline_comments_receive_backend_fallbacks(
    schema_items: list[dict[str, object]],
) -> None:
    by_path = _by_path(schema_items)
    expected = {
        "plugin_runtime.default_timeout_seconds": "插件默认超时",
        "auth.api_keys[].key": "API Key 值",
        "plugins[].config.model_name": "压缩模型名称",
        "media_optimization.download_timeouts.image": "图片下载超时",
        "debug.plugins.per_plugin.cost_tracker": "成本追踪调试开关",
        "generation_optimization.draft_workflow.comfyui.video_cfg": "CFG",
    }
    for path, fragment in expected.items():
        assert path in by_path
        assert fragment in str(by_path[path]["description"])


def test_yaml_parser_preserves_hashes_inside_quoted_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = tmp_path / "config.yaml.template"
    template.write_text(
        'providers:\n'
        '  custom:\n'
        '    base_url: "https://example.test/path#fragment"  # API 基地址\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_GATEWAY_CONFIG_TEMPLATE_PATH", str(template))

    items = parse_template_schema(str(tmp_path / "config.yaml"))

    by_path = _by_path(items)
    assert by_path["providers.*.base_url"]["description"] == "API 基地址"
    assert by_path["providers.*.base_url"]["value_type"] == "string"
