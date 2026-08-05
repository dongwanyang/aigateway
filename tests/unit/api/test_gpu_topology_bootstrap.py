from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def _module():
    # Import through the installed package namespace so pytest-cov's
    # ``--cov=aigateway_api`` source filter attributes execution correctly.
    from aigateway_api import gpu_topology_bootstrap

    return gpu_topology_bootstrap


def _current_gpu(uuid: str = "GPU-new", index: int = 0):
    return {
        "index": index,
        "uuid": uuid,
        "name": "Current GPU",
        "memory_total_mb": 24576,
        "memory_free_mb": 23552,
        "total_memory_gb": 24.0,
        "free_memory_gb": 23.0,
    }


def test_bootstrap_remaps_stale_uuid_topology_and_cuda_visibility(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module()
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
    assert scheduler["inventory_source"] == "host_generated"
    assert scheduler["inventory_fingerprint"]
    assert {
        item["device_uuid"] for item in scheduler["workers"]
    }.issubset({item["uuid"] for item in scheduler["devices"]})
    assert module.os.environ["CUDA_VISIBLE_DEVICES"] == "0"
    assert module.bootstrap_gpu_topology() is False


def test_bootstrap_preserves_valid_uuid_visibility(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "gpu_scheduler": {
                    "enabled": True,
                    "gateway_devices": ["GPU-current"],
                    "comfyui_devices": "auto",
                    "devices": [{"index": 0, "uuid": "GPU-current"}],
                    "workers": [],
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        module,
        "_discover_devices",
        lambda: [_current_gpu("GPU-current", 0)],
    )
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "GPU-current")

    assert module.bootstrap_gpu_topology() is True
    assert module.os.environ["CUDA_VISIBLE_DEVICES"] == "GPU-current"


def test_bootstrap_maps_stale_visibility_to_only_its_previous_slot(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "gpu_scheduler": {
                    "enabled": True,
                    "gateway_devices": "auto",
                    "comfyui_devices": "auto",
                    "devices": [
                        {"index": 0, "uuid": "GPU-old-a"},
                        {"index": 1, "uuid": "GPU-old-b"},
                    ],
                    "workers": [],
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        module,
        "_discover_devices",
        lambda: [
            _current_gpu("GPU-new-a", 0),
            _current_gpu("GPU-new-b", 1),
        ],
    )
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "GPU-old-b")

    assert module.bootstrap_gpu_topology() is True
    assert module.os.environ["CUDA_VISIBLE_DEVICES"] == "1"


def test_bootstrap_migrates_legacy_worker_using_previous_device_index(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module()
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
    module = _module()
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


def test_bootstrap_fails_closed_when_override_cannot_be_mapped(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module()
    config_path = tmp_path / "config.yaml"
    original = {
        "gpu_scheduler": {
            "enabled": True,
            "gateway_devices": "auto",
            "comfyui_devices": "auto",
            "device_overrides": [
                {"uuid": "GPU-orphan", "enabled": False}
            ],
            "devices": [{"index": 0, "uuid": "GPU-old"}],
            "workers": [],
        }
    }
    config_path.write_text(
        yaml.safe_dump(original, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setattr(
        module,
        "_discover_devices",
        lambda: [_current_gpu("GPU-new", 0)],
    )
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", str(config_path))

    with pytest.raises(RuntimeError, match="overrides reference devices"):
        module.bootstrap_gpu_topology()

    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == original


def test_bootstrap_rejects_missing_inventory_for_persisted_topology(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module()
    config_path = tmp_path / "config.yaml"
    original = {
        "gpu_scheduler": {
            "enabled": True,
            "devices": [{"index": 0, "uuid": "GPU-old"}],
            "workers": [],
        }
    }
    config_path.write_text(
        yaml.safe_dump(original, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setattr(module, "_discover_devices", lambda: [])
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", str(config_path))

    with pytest.raises(RuntimeError, match="requires a current NVIDIA inventory"):
        module.bootstrap_gpu_topology()

    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == original


def test_bootstrap_is_noop_without_inventory_or_local_topology(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module()
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


def test_bootstrap_locked_write_failure_restores_original_config(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module()
    config_path = tmp_path / "config.yaml"
    original = {
        "gpu_scheduler": {
            "enabled": True,
            "gateway_devices": ["GPU-old"],
            "comfyui_devices": "auto",
            "devices": [{"index": 0, "uuid": "GPU-old"}],
            "workers": [],
        }
    }
    original_text = yaml.safe_dump(original, sort_keys=False)
    config_path.write_text(original_text, encoding="utf-8")
    monkeypatch.setattr(
        module,
        "_discover_devices",
        lambda: [_current_gpu("GPU-new", 0)],
    )
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", str(config_path))

    real_fsync = module.os.fsync
    calls = 0

    def _fail_first_fsync(descriptor):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("fsync failed")
        return real_fsync(descriptor)

    monkeypatch.setattr(module.os, "fsync", _fail_first_fsync)
    with pytest.raises(OSError, match="fsync failed"):
        module.bootstrap_gpu_topology()

    assert config_path.read_text(encoding="utf-8") == original_text
