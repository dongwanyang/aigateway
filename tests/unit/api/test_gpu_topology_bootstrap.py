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


def _module(name: str):
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _current_gpu(uuid: str = "GPU-new", index: int = 0):
    return {
        "index": index,
        "uuid": uuid,
        "name": "Current GPU",
        "total_memory_gb": 24.0,
        "free_memory_gb": 23.0,
    }


def test_bootstrap_remaps_stale_uuid_topology_and_cuda_visibility(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module("gpu_topology_bootstrap_remap")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "gpu_scheduler": {
                    "enabled": True,
                    "gateway_devices": ["GPU-old"],
                    "comfyui_devices": ["GPU-old"],
                    "device_overrides": [
                        {
                            "uuid": "GPU-old",
                            "enabled": True,
                            "capabilities": ["image", "video"],
                        }
                    ],
                    "devices": [
                        {
                            "index": 0,
                            "uuid": "GPU-old",
                            "name": "Old GPU",
                            "total_memory_gb": 16.0,
                            "free_memory_gb": 16.0,
                        }
                    ],
                    "workers": [
                        {
                            "worker_id": "comfyui-gpu-0",
                            "logical_index": 0,
                            "device_uuid": "GPU-old",
                            "server_url": "http://comfyui:8188",
                            "capabilities": ["image", "video", "upscale"],
                        }
                    ],
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "_discover_devices", lambda: [_current_gpu()])
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "GPU-old")

    assert module.bootstrap_gpu_topology() is True

    scheduler = yaml.safe_load(config_path.read_text(encoding="utf-8"))[
        "gpu_scheduler"
    ]
    assert scheduler["gateway_devices"] == ["GPU-new"]
    assert scheduler["comfyui_devices"] == ["GPU-new"]
    assert scheduler["device_overrides"][0]["uuid"] == "GPU-new"
    assert scheduler["devices"][0]["uuid"] == "GPU-new"
    assert scheduler["workers"][0]["device_uuid"] == "GPU-new"
    assert scheduler["workers"][0]["logical_index"] == 0
    assert scheduler["inventory_source"] == "gateway_startup_discovery"
    assert scheduler["inventory_fingerprint"]
    assert {
        item["device_uuid"] for item in scheduler["workers"]
    }.issubset({item["uuid"] for item in scheduler["devices"]})
    assert module.os.environ["CUDA_VISIBLE_DEVICES"] == "0"


def test_bootstrap_migrates_legacy_worker_using_previous_device_index(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module("gpu_topology_bootstrap_legacy")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "gpu_scheduler": {
                    "enabled": True,
                    "gateway_devices": "auto",
                    "comfyui_devices": "auto",
                    "devices": [{"index": 1, "uuid": "GPU-old"}],
                    "workers": [
                        {
                            "worker_id": "comfyui-gpu-0",
                            "device_uuid": "GPU-old",
                            "server_url": "http://comfyui:8188",
                        }
                    ],
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    current = [_current_gpu("GPU-zero", 0), _current_gpu("GPU-new", 1)]
    monkeypatch.setattr(module, "_discover_devices", lambda: current)
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", str(config_path))
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)

    assert module.bootstrap_gpu_topology() is True

    worker = yaml.safe_load(config_path.read_text(encoding="utf-8"))[
        "gpu_scheduler"
    ]["workers"][0]
    assert worker["logical_index"] == 1
    assert worker["device_uuid"] == "GPU-new"


def test_bootstrap_fails_closed_when_worker_cannot_be_mapped(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module("gpu_topology_bootstrap_fail_closed")
    config_path = tmp_path / "config.yaml"
    original = {
        "gpu_scheduler": {
            "enabled": True,
            "gateway_devices": "auto",
            "comfyui_devices": "auto",
            "devices": [],
            "workers": [
                {
                    "worker_id": "orphan-worker",
                    "device_uuid": "GPU-unknown",
                    "server_url": "http://comfyui:8188",
                }
            ],
        }
    }
    config_path.write_text(
        yaml.safe_dump(original, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setattr(
        module,
        "_discover_devices",
        lambda: [_current_gpu("GPU-a", 0), _current_gpu("GPU-b", 1)],
    )
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", str(config_path))

    with pytest.raises(RuntimeError, match="cannot be paired"):
        module.bootstrap_gpu_topology()

    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == original


def test_bootstrap_is_noop_without_nvidia_inventory(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module("gpu_topology_bootstrap_no_gpu")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"gpu_scheduler": {"enabled": True}}),
        encoding="utf-8",
    )
    before = config_path.read_text(encoding="utf-8")
    monkeypatch.setattr(module, "_discover_devices", lambda: [])
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", str(config_path))

    assert module.bootstrap_gpu_topology() is False
    assert config_path.read_text(encoding="utf-8") == before
