from __future__ import annotations

import json

import pytest
from aigateway_api.local_generation import (
    builtin_presets,
    delete_custom_preset,
    dependency_status,
    discovered_checkpoint_presets,
    load_custom_presets,
    merge_generation_presets,
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


def test_discovers_only_trusted_profiled_checkpoints_as_selectable(tmp_path):
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
            "allowed_checkpoints": [
                "default.safetensors",
                "portraits/cinematic.ckpt",
            ],
            "checkpoint_vram_gb": {"portraits/cinematic.ckpt": 11},
        }
    )

    assert len(presets) == 1
    preset = presets[0]
    assert preset["source"] == "discovered"
    assert preset["name"] == "portraits/cinematic（本地 Checkpoint）"
    assert preset["selectable"] is True
    assert preset["enabled"] is True
    assert preset["workflow_family"] == "sdxl"
    assert preset["model_name"] == "portraits/cinematic.ckpt"
    assert preset["required_vram_gb"] == 11
    assert checkpoint_name_from_preset_id(preset["id"]) == preset["model_name"]


def test_discovered_checkpoint_without_server_trust_is_detection_only(tmp_path):
    checkpoints = tmp_path / "checkpoints"
    checkpoints.mkdir()
    (checkpoints / "unknown.safetensors").write_bytes(b"model")

    [preset] = discovered_checkpoint_presets(
        {"models_path": str(tmp_path), "allowed_checkpoints": []}
    )

    assert preset["selectable"] is False
    assert preset["enabled"] is False
    assert preset["workflow_family"] == "unknown"
    assert preset["required_vram_gb"] is None
    assert preset["configuration_status"] == "configuration_error"
    assert any(
        error.startswith("checkpoint_not_allowlisted:")
        for error in preset["configuration_errors"]
    )
    assert any(
        error.startswith("checkpoint_vram_unconfigured:")
        for error in preset["configuration_errors"]
    )


def test_custom_presets_cannot_use_checkpoint_namespace(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_GENERATION_PRESETS_DIR", str(tmp_path))

    with pytest.raises(ValueError, match="reserved checkpoint namespace"):
        save_custom_preset({"id": "checkpoint.YWJj"})


def test_preset_merge_is_globally_id_unique():
    merged = merge_generation_presets(
        [{"id": "same", "source": "builtin"}],
        [{"id": "same", "source": "custom"}, {"id": "other"}],
    )

    assert merged == [
        {"id": "same", "source": "builtin"},
        {"id": "other"},
    ]


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


def test_multistage_presets_require_the_sdxl_stage():
    presets = {
        preset["id"]: preset
        for preset in builtin_presets(
            {
                "checkpoint_name": "default.safetensors",
                "allowed_checkpoints": [],
                "qwen_image_enabled": True,
                "qwen_image_diffusion_model": "qwen.safetensors",
                "qwen_image_text_encoder": "qwen-clip.safetensors",
                "qwen_image_vae": "qwen-vae.safetensors",
                "video_enabled": True,
                "video_diffusion_model": "wan.safetensors",
                "video_text_encoder": "wan-clip.safetensors",
                "video_vae": "wan-vae.safetensors",
            }
        )
    }

    for preset_id in ("qwen-image", "wan2.2-ti2v-5b"):
        preset = presets[preset_id]
        assert preset["enabled"] is False
        assert "checkpoints/default.safetensors" in preset["dependencies"]["models"]
        assert "CheckpointLoaderSimple" in preset["dependencies"]["nodes"]
        if preset_id == "wan2.2-ti2v-5b":
            assert "Wan22ImageToVideoLatent" in preset["dependencies"]["nodes"]
            assert "CreateVideo" in preset["dependencies"]["nodes"]
            assert "SaveVideo" in preset["dependencies"]["nodes"]
            assert "WanImageToVideo" not in preset["dependencies"]["nodes"]
            assert "SaveAnimatedWEBP" not in preset["dependencies"]["nodes"]
        assert (
            "checkpoint_not_allowlisted:default.safetensors"
            in preset["configuration_errors"]
        )
