from __future__ import annotations

from pathlib import Path

import yaml
from aigateway_core.shared.integration_configs import ComfyUIConfig


REPO_ROOT = Path(__file__).resolve().parents[3]


def _comfyui_config(path: Path) -> dict[str, object]:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    return config["generation_optimization"]["draft_workflow"]["comfyui"]


def test_comfyui_video_fallback_matches_five_second_default() -> None:
    config = ComfyUIConfig()

    assert config.video_fps == 8.0
    assert config.video_frames == 41


def test_runtime_config_video_fallback_matches_code_default() -> None:
    comfyui = _comfyui_config(REPO_ROOT / "config.yaml")

    assert comfyui["video_fps"] == 8
    assert comfyui["video_frames"] == ComfyUIConfig().video_frames


def test_config_template_video_fallback_matches_code_default() -> None:
    comfyui = _comfyui_config(REPO_ROOT / "config.yaml.template")

    assert comfyui["video_fps"] == 8
    assert comfyui["video_frames"] == ComfyUIConfig().video_frames
