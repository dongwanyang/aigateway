from __future__ import annotations

import json

import pytest
from aigateway_api.local_generation import (
    delete_custom_preset,
    dependency_status,
    discovered_checkpoint_presets,
    load_custom_presets,
    save_custom_preset,
)
from aigateway_core.shared.comfyui_model_discovery import (
    checkpoint_name_from_preset_id,
    checkpoint_preset_id,
    discover_checkpoint_models,
    validate_checkpoint_file,
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
        "configuration_errors": [],
    }


def test_dependency_status_surfaces_configuration_errors(tmp_path):
    status = dependency_status(
        {
            "dependencies": {"models": [], "nodes": []},
            "configuration_errors": ["config_missing:workflow_version"],
        },
        str(tmp_path),
        set(),
    )
    assert status == {
        "missing_models": ["config_missing:workflow_version"],
        "missing_nodes": [],
        "configuration_errors": ["config_missing:workflow_version"],
    }


def test_discovers_installed_checkpoints_and_builds_selectable_presets(tmp_path):
    checkpoints = tmp_path / "checkpoints"
    (checkpoints / "portraits").mkdir(parents=True)
    (checkpoints / "default.safetensors").write_bytes(b"default")
    (checkpoints / "portraits" / "cinematic.ckpt").write_bytes(b"model")
    (checkpoints / "ignore.txt").write_text("not a checkpoint")

    assert discover_checkpoint_models(str(tmp_path)) == [
        "default.safetensors",
        "portraits/cinematic.ckpt",
    ]
    presets = discovered_checkpoint_presets(
        {
            "models_path": str(tmp_path),
            "checkpoint_name": "default.safetensors",
            "sdxl_required_vram_gb": 9,
        }
    )

    assert len(presets) == 1
    preset = presets[0]
    assert preset["source"] == "discovered"
    assert preset["selectable"] is True
    assert preset["model_name"] == "portraits/cinematic.ckpt"
    assert preset["required_vram_gb"] == 9
    assert checkpoint_name_from_preset_id(preset["id"]) == preset["model_name"]


def test_checkpoint_preset_validation_fails_closed_for_unsafe_paths(tmp_path):
    checkpoints = tmp_path / "checkpoints"
    checkpoints.mkdir()
    model = checkpoints / "safe.safetensors"
    model.write_bytes(b"model")

    preset_id = checkpoint_preset_id("safe.safetensors")
    assert checkpoint_name_from_preset_id(preset_id) == "safe.safetensors"
    assert validate_checkpoint_file(str(tmp_path), "safe.safetensors") == "safe.safetensors"

    with pytest.raises(ValueError):
        checkpoint_preset_id("../outside.safetensors")
    with pytest.raises(ValueError):
        checkpoint_name_from_preset_id("checkpoint.not-valid-base64!")
    with pytest.raises(ValueError):
        validate_checkpoint_file(str(tmp_path), "missing.safetensors")
