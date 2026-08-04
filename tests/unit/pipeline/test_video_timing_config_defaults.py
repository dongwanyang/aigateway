from __future__ import annotations

from pathlib import Path

import yaml
from aigateway_core.pipelines.generation._common.config import (
    GenerationOptimizationConfig,
)
from aigateway_core.shared.integration_configs import ComfyUIConfig


REPO_ROOT = Path(__file__).resolve().parents[3]


def _draft_workflow_config(path: Path) -> dict[str, object]:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    return config["generation_optimization"]["draft_workflow"]


def _comfyui_config(path: Path) -> dict[str, object]:
    return _draft_workflow_config(path)["comfyui"]


def test_generation_timing_policy_defaults() -> None:
    timing = GenerationOptimizationConfig().draft_workflow

    assert timing.video_default_duration_seconds == 5
    assert timing.video_supported_durations_seconds == (3, 5, 8)
    assert timing.video_default_fps == 8
    assert timing.video_max_fps == 60
    assert timing.video_min_frames == 1
    assert timing.video_max_frames == 481


def test_generation_timing_policy_loads_from_yaml_values() -> None:
    config = GenerationOptimizationConfig.load_from_dict(
        {
            "draft_workflow": {
                "video_default_duration_seconds": 3,
                "video_supported_durations_seconds": [3, 5],
                "video_default_fps": 12,
                "video_max_fps": 24,
                "video_min_frames": 5,
                "video_max_frames": 241,
            },
        },
    )
    timing = config.draft_workflow

    assert timing.video_default_duration_seconds == 3
    assert timing.video_supported_durations_seconds == (3, 5)
    assert timing.video_default_fps == 12
    assert timing.video_max_fps == 24
    assert timing.video_min_frames == 5
    assert timing.video_max_frames == 241
    assert config.validate() == []


def test_generation_timing_policy_rejects_cross_field_conflicts() -> None:
    config = GenerationOptimizationConfig()
    timing = config.draft_workflow
    timing.video_supported_durations_seconds = (3, 8)
    timing.video_default_duration_seconds = 5
    timing.video_default_fps = 61
    timing.video_max_fps = 60
    timing.video_min_frames = 100
    timing.video_max_frames = 80

    errors = config.validate()

    assert any("video_default_duration_seconds 必须属于" in error for error in errors)
    assert any("video_default_fps 不得大于" in error for error in errors)
    assert any("video_min_frames 不得大于" in error for error in errors)


def test_generation_timing_policy_rejects_invalid_duration_allowlist() -> None:
    config = GenerationOptimizationConfig()
    config.draft_workflow.video_supported_durations_seconds = ()

    errors = config.validate()

    assert any("video_supported_durations_seconds 必须是非空" in error for error in errors)


def test_generation_timing_policy_rejects_unrepresentable_default_frames() -> None:
    config = GenerationOptimizationConfig()
    config.draft_workflow.video_max_frames = 40

    errors = config.validate()

    assert any("默认视频时序归一化后" in error for error in errors)


def test_runtime_and_template_timing_policy_match_code_defaults() -> None:
    timing = GenerationOptimizationConfig().draft_workflow

    for path in (REPO_ROOT / "config.yaml", REPO_ROOT / "config.yaml.template"):
        configured = _draft_workflow_config(path)
        assert configured["video_default_duration_seconds"] == (
            timing.video_default_duration_seconds
        )
        assert tuple(configured["video_supported_durations_seconds"]) == (
            timing.video_supported_durations_seconds
        )
        assert configured["video_default_fps"] == timing.video_default_fps
        assert configured["video_max_fps"] == timing.video_max_fps
        assert configured["video_min_frames"] == timing.video_min_frames
        assert configured["video_max_frames"] == timing.video_max_frames


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
