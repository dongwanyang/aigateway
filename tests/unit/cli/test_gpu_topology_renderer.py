from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts/render-gpu-topology.py"


def _module():
    spec = importlib.util.spec_from_file_location("gpu_topology_renderer", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("count", [1, 2, 3])
def test_render_topology_creates_one_uuid_bound_worker_per_gpu(count: int) -> None:
    devices = [
        {
            "index": index,
            "uuid": f"GPU-{index}",
            "name": f"GPU model {index}",
            "memory_total_mb": 16384 * (index + 1),
        }
        for index in range(count)
    ]
    compose, workers = _module().render_topology(devices)
    assert len(workers) == count
    assert [item["device_uuid"] for item in workers] == [
        f"GPU-{index}" for index in range(count)
    ]
    assert compose["services"]["gateway"]["environment"]["CUDA_VISIBLE_DEVICES"] == "all"
    for index, worker in enumerate(workers):
        service = "comfyui" if index == 0 else f"comfyui-gpu-{index}"
        assert compose["services"][service]["environment"]["CUDA_VISIBLE_DEVICES"] == worker["device_uuid"]
        assert (
            compose["services"][service]["environment"][
                "COMFYUI_DISABLE_DYNAMIC_VRAM"
            ]
            == "${COMFYUI_DISABLE_DYNAMIC_VRAM:-true}"
        )
        volumes = compose["services"][service]["volumes"]
        assert any(value.endswith("/models:ro") for value in volumes)
        expected_output = (
            "/comfyui}/output"
            if index == 0
            else f"workers/comfyui-gpu-{index}/output"
        )
        assert any(expected_output in value for value in volumes)


def test_select_comfyui_devices_uses_uuid_pool_and_disabled_overrides() -> None:
    devices = [
        {"index": 0, "uuid": "GPU-a"},
        {"index": 1, "uuid": "GPU-b"},
        {"index": 2, "uuid": "GPU-c"},
    ]
    selected = _module().select_comfyui_devices(
        devices,
        {
            "comfyui_devices": ["GPU-a", "GPU-c"],
            "device_overrides": [{"uuid": "GPU-c", "enabled": False}],
        },
    )
    assert selected == [{"index": 0, "uuid": "GPU-a"}]


def test_render_topology_can_enable_dynamic_vram_from_config() -> None:
    compose, _ = _module().render_topology(
        [
            {
                "index": 0,
                "uuid": "GPU-a",
                "name": "GPU A",
                "memory_total_mb": 16384,
            }
        ],
        {"comfyui_dynamic_vram_enabled": True},
    )

    assert (
        compose["services"]["comfyui"]["environment"][
            "COMFYUI_DISABLE_DYNAMIC_VRAM"
        ]
        == "${COMFYUI_DISABLE_DYNAMIC_VRAM:-false}"
    )

def test_main_preserves_disabled_scheduler(tmp_path, monkeypatch) -> None:
    import sys

    import yaml

    inventory = tmp_path / "inventory.yaml"
    runtime = tmp_path / "config.yaml"
    output = tmp_path / "compose.yaml"
    inventory.write_text(
        yaml.safe_dump(
            [
                {
                    "index": 0,
                    "uuid": "GPU-a",
                    "name": "GPU A",
                    "memory_total_mb": 16384,
                }
            ]
        ),
        encoding="utf-8",
    )
    runtime.write_text(
        yaml.safe_dump({"gpu_scheduler": {"enabled": False}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--output-compose",
            str(output),
            "--runtime-config",
            str(runtime),
            "--inventory",
            str(inventory),
        ],
    )

    assert _module().main() == 0
    rendered = yaml.safe_load(runtime.read_text(encoding="utf-8"))
    assert rendered["gpu_scheduler"]["enabled"] is False
