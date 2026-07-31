from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import yaml

from aigateway_api.regression_fixes import (
    PluginToggleRequest,
    _parse_prometheus_labels,
    safe_flocked_inplace_write,
)


def test_safe_flocked_inplace_write_replaces_complete_yaml(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("old: true\n", encoding="utf-8")

    safe_flocked_inplace_write(str(path), {"server": {"port": 8000}})

    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {
        "server": {"port": 8000}
    }


def test_plugin_toggle_requires_real_boolean() -> None:
    assert PluginToggleRequest(name="cache", enabled=False).enabled is False

    for value in ("false", 0, 1, [], {}):
        try:
            PluginToggleRequest(name="cache", enabled=value)
        except Exception:
            pass
        else:
            raise AssertionError(f"accepted non-boolean value: {value!r}")


def test_prometheus_label_parser_preserves_quoted_commas_and_equals() -> None:
    assert _parse_prometheus_labels('model="a,b",provider="x=y"') == {
        "model": "a,b",
        "provider": "x=y",
    }
