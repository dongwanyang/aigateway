from __future__ import annotations

import json

import pytest
from aigateway_api.local_generation import (
    delete_custom_preset,
    dependency_status,
    load_custom_presets,
    save_custom_preset,
)


def test_custom_preset_atomic_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_GENERATION_PRESETS_DIR", str(tmp_path))
    preset = {
        "id": "my-workflow",
        "name": "My workflow",
        "builtin": False,
        "dependencies": {"models": [], "nodes": []},
        "workflow": {"1": {"class_type": "SaveImage", "inputs": {}}},
    }
    save_custom_preset(preset)

    assert load_custom_presets() == [preset]
    assert json.loads((tmp_path / "my-workflow.json").read_text()) == preset
    assert not list(tmp_path.glob(".my-workflow.*"))
    assert delete_custom_preset("my-workflow") is True
    assert delete_custom_preset("my-workflow") is False


@pytest.mark.parametrize("preset_id", ["../escape", "/absolute", "UPPER"])
def test_custom_preset_rejects_unsafe_id(tmp_path, monkeypatch, preset_id):
    monkeypatch.setenv("AI_GATEWAY_GENERATION_PRESETS_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        save_custom_preset({"id": preset_id})


def test_dependency_status_rejects_traversal_and_reports_missing(tmp_path):
    (tmp_path / "upscale_models").mkdir()
    (tmp_path / "upscale_models" / "ok.pth").write_bytes(b"model")
    status = dependency_status(
        {
            "dependencies": {
                "models": ["upscale_models/ok.pth", "../secret"],
                "nodes": ["SaveImage", "MissingNode"],
            }
        },
        str(tmp_path),
        {"SaveImage"},
    )
    assert status == {
        "missing_models": ["../secret"],
        "missing_nodes": ["MissingNode"],
    }
