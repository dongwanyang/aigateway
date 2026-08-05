from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "render-gpu-topology.py"


def _module():
    spec = importlib.util.spec_from_file_location(
        "gpu_topology_renderer_malformed", SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_host_reconciliation_rejects_scalar_selector() -> None:
    renderer = _module()
    inventory = [
        {
            "index": 0,
            "uuid": "GPU-current",
            "name": "GPU",
            "memory_total_mb": 16384,
        }
    ]

    with pytest.raises(RuntimeError, match="gateway_devices must be"):
        renderer.reconcile_scheduler_device_references(
            inventory,
            {
                "gateway_devices": "GPU-old",
                "comfyui_devices": "auto",
                "devices": [{"index": 0, "uuid": "GPU-old"}],
            },
        )


def test_device_selection_rejects_scalar_selector_defensively() -> None:
    renderer = _module()
    with pytest.raises(RuntimeError, match="comfyui_devices must be"):
        renderer.select_comfyui_devices(
            [{"index": 0, "uuid": "GPU-current"}],
            {"comfyui_devices": "GPU-current"},
        )
