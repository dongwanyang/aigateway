from __future__ import annotations

from pathlib import Path

import yaml
from aigateway_core.shared.integration_configs import ComfyUIConfig


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_comfyui_video_fallback_matches_five_second_default() -> None:
    config = ComfyUIConfig()

    assert config.video_fps == 8.0
    assert config.video_frames == 41


def test_runtime_config_video_fallback_matches_code_default() -> None:
    runtime_config = yaml.safe_load(
        (REPO_ROOT / "config.yaml").read_text(encoding="utf-8"),
    )
    comfyui = runtime_config["generation_optimization"]["draft_workflow"]["comfyui"]

    assert comfyui["video_fps"] == 8
    assert comfyui["video_frames"] == ComfyUIConfig().video_frames
