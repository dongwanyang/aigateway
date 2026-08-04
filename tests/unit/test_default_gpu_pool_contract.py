"""Default deployment ownership contract for the local ComfyUI endpoint."""
from __future__ import annotations

from pathlib import Path

import yaml


BASE = Path(__file__).resolve().parents[2]


def _load(filename: str) -> dict:
    return yaml.safe_load((BASE / filename).read_text(encoding="utf-8")) or {}


def test_repository_config_treats_local_comfyui_as_scheduler_managed() -> None:
    config = _load("config.yaml")
    scheduler = config["gpu_scheduler"]
    comfy = config["generation_optimization"]["draft_workflow"]["comfyui"]

    assert scheduler["enabled"] is True
    assert comfy["server_url"] == "http://comfyui:8188"
    assert comfy["scheduler_managed"] is True


def test_config_template_treats_local_comfyui_as_scheduler_managed() -> None:
    config = _load("config.yaml.template")
    scheduler = config["gpu_scheduler"]
    comfy = config["generation_optimization"]["draft_workflow"]["comfyui"]

    assert scheduler["enabled"] is True
    assert comfy["server_url"] == "http://comfyui:8188"
    assert comfy["scheduler_managed"] is True
