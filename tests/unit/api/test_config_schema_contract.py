from __future__ import annotations

from pathlib import Path

from aigateway_api import routes
from aigateway_api.config_schema import parse_template_schema


def test_gateway_image_contains_runtime_config_template() -> None:
    dockerfile = (
        Path(__file__).resolve().parents[3]
        / "aigateway-api"
        / "Dockerfile"
    ).read_text(encoding="utf-8")
    assert "COPY config.yaml.template /app/config.yaml.template" in dockerfile


def test_template_schema_preserves_list_context(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    template_path = tmp_path / "config.yaml.template"
    config_path.write_text("plugins: []\n", encoding="utf-8")
    template_path.write_text(
        """plugins:  # Plugin list
  - name: pii_detector  # Plugin name
    enabled: true  # Plugin enabled
    config:
      strategy: sanitize  # PII strategy
providers:
  demo:
    base_url: "https://example.test/#fragment"  # Provider URL
server:
  cors_origins:  # CORS origins
    - https://panel.example
""",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "AI_GATEWAY_CONFIG_TEMPLATE_PATH",
        str(template_path),
    )

    items = parse_template_schema(str(config_path))
    by_path = {item["path"]: item["description"] for item in items}

    assert by_path["plugins"] == "Plugin list"
    assert by_path["plugins[].name"] == "Plugin name"
    assert by_path["plugins[].enabled"] == "Plugin enabled"
    assert by_path["plugins[].config.strategy"] == "PII strategy"
    assert by_path["providers.demo.base_url"] == "Provider URL"
    assert by_path["server.cors_origins"] == "CORS origins"


def test_routes_use_yaml_aware_schema_parser() -> None:
    assert routes._parse_template_schema is parse_template_schema
