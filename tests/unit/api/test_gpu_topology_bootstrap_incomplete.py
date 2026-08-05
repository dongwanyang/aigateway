from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = (
    REPO_ROOT
    / "aigateway-api"
    / "src"
    / "aigateway_api"
    / "gpu_topology_bootstrap.py"
)


def _module():
    spec = importlib.util.spec_from_file_location(
        "gpu_topology_bootstrap_incomplete", MODULE_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gpu():
    return {
        "index": 0,
        "uuid": "GPU-current",
        "name": "Current GPU",
        "total_memory_gb": 24.0,
    }


def test_bootstrap_rejects_expected_local_pool_without_workers(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module()
    config_path = tmp_path / "config.yaml"
    original = {
        "deployment": {"shared_gpu": True},
        "gpu_scheduler": {
            "enabled": True,
            "gateway_devices": "auto",
            "comfyui_devices": "auto",
            "devices": [{"index": 0, "uuid": "GPU-old"}],
            "workers": [],
        },
    }
    config_path.write_text(
        yaml.safe_dump(original, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setattr(module, "_discover_devices", lambda: [_gpu()])
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", str(config_path))

    with pytest.raises(RuntimeError, match="local ComfyUI pool has no workers"):
        module.bootstrap_gpu_topology()

    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == original


def test_bootstrap_allows_gateway_only_scheduler_without_workers(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "deployment": {"shared_gpu": False},
                "gpu_scheduler": {
                    "enabled": True,
                    "gateway_devices": "auto",
                    "comfyui_devices": "auto",
                    "devices": [{"index": 0, "uuid": "GPU-old"}],
                    "workers": [],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "_discover_devices", lambda: [_gpu()])
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", str(config_path))

    assert module.bootstrap_gpu_topology() is True
    scheduler = yaml.safe_load(config_path.read_text(encoding="utf-8"))[
        "gpu_scheduler"
    ]
    assert scheduler["devices"][0]["uuid"] == "GPU-current"
    assert scheduler["workers"] == []
