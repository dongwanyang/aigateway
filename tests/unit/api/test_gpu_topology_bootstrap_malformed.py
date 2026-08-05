from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def _module():
    from aigateway_api import gpu_topology_bootstrap

    return gpu_topology_bootstrap


def _inventory():
    return [
        {
            "index": 0,
            "uuid": "GPU-current-a",
            "name": "GPU A",
            "memory_total_mb": 16384,
            "memory_free_mb": 16000,
        },
        {
            "index": 1,
            "uuid": "GPU-current-b",
            "name": "GPU B",
            "memory_total_mb": 24576,
            "memory_free_mb": 24000,
        },
    ]


def _write(tmp_path: Path, scheduler: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({"gpu_scheduler": scheduler}, sort_keys=False),
        encoding="utf-8",
    )
    return path


def test_bootstrap_rejects_scalar_selector_instead_of_expanding_to_all_gpus(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module()
    path = _write(
        tmp_path,
        {
            "enabled": True,
            "gateway_devices": "GPU-old",
            "comfyui_devices": "auto",
            "devices": [{"index": 0, "uuid": "GPU-old"}],
            "workers": [],
        },
    )
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", str(path))
    monkeypatch.setattr(module, "_discover_devices", _inventory)

    with pytest.raises(RuntimeError, match="selector must be"):
        module.bootstrap_gpu_topology()


def test_bootstrap_rejects_mixed_malformed_worker_list(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module()
    path = _write(
        tmp_path,
        {
            "enabled": True,
            "gateway_devices": "auto",
            "comfyui_devices": "auto",
            "devices": [{"index": 0, "uuid": "GPU-old"}],
            "workers": [
                {
                    "worker_id": "comfyui-gpu-0",
                    "logical_index": 0,
                    "device_uuid": "GPU-old",
                },
                "not-a-worker",
            ],
        },
    )
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", str(path))
    monkeypatch.setattr(module, "_discover_devices", _inventory)

    with pytest.raises(RuntimeError, match="worker is malformed at index 1"):
        module.bootstrap_gpu_topology()


def test_bootstrap_does_not_default_missing_previous_index_to_zero(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module()
    path = _write(
        tmp_path,
        {
            "enabled": True,
            "gateway_devices": ["GPU-old"],
            "comfyui_devices": "auto",
            "devices": [{"uuid": "GPU-old"}],
            "workers": [],
        },
    )
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", str(path))
    monkeypatch.setattr(module, "_discover_devices", _inventory)

    with pytest.raises(RuntimeError, match="cannot be remapped"):
        module.bootstrap_gpu_topology()
