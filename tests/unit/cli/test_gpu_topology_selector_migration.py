from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RENDERER_PATH = REPO_ROOT / "scripts" / "render-gpu-topology.py"
CONTROLLER_PATH = REPO_ROOT / "scripts" / "gpu-topology-controller.py"


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _old_scheduler() -> dict:
    return {
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
                "index": 1,
                "uuid": "GPU-old",
                "name": "Old GPU",
                "total_memory_gb": 16.0,
                "free_memory_gb": 16.0,
            }
        ],
        "workers": [
            {
                "worker_id": "comfyui-gpu-0",
                "logical_index": 1,
                "device_uuid": "GPU-old",
                "server_url": "http://comfyui:8188",
            }
        ],
    }


def _new_inventory() -> list[dict]:
    return [
        {
            "index": 0,
            "uuid": "GPU-other",
            "name": "Other GPU",
            "memory_total_mb": 12288,
        },
        {
            "index": 1,
            "uuid": "GPU-new",
            "name": "Replacement GPU",
            "memory_total_mb": 24576,
        },
    ]


def test_renderer_main_migrates_stale_explicit_selectors_before_selection(
    tmp_path: Path, monkeypatch
) -> None:
    renderer = _module(RENDERER_PATH, "selector_renderer_main")
    inventory_path = tmp_path / "inventory.yaml"
    runtime_path = tmp_path / "config.yaml"
    compose_path = tmp_path / "compose.yaml"
    inventory_path.write_text(
        yaml.safe_dump(_new_inventory(), sort_keys=False),
        encoding="utf-8",
    )
    runtime_path.write_text(
        yaml.safe_dump({"gpu_scheduler": _old_scheduler()}, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(RENDERER_PATH),
            "--output-compose",
            str(compose_path),
            "--runtime-config",
            str(runtime_path),
            "--inventory",
            str(inventory_path),
        ],
    )

    assert renderer.main() == 0

    scheduler = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))[
        "gpu_scheduler"
    ]
    assert scheduler["gateway_devices"] == ["GPU-new"]
    assert scheduler["comfyui_devices"] == ["GPU-new"]
    assert scheduler["device_overrides"][0]["uuid"] == "GPU-new"
    assert scheduler["workers"][0]["device_uuid"] == "GPU-new"
    assert scheduler["workers"][0]["logical_index"] == 1
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    assert (
        compose["services"]["comfyui"]["environment"][
            "CUDA_VISIBLE_DEVICES"
        ]
        == "1"
    )


def test_controller_migrates_stale_explicit_selectors_before_selection(
    tmp_path: Path, monkeypatch
) -> None:
    renderer = _module(RENDERER_PATH, "selector_renderer_controller")
    controller = _module(CONTROLLER_PATH, "selector_controller")
    runtime_dir = tmp_path / ".aigateway" / "runtime"
    runtime_dir.mkdir(parents=True)
    runtime_config = runtime_dir / "config.yaml"
    runtime_config.write_text(
        yaml.safe_dump({"gpu_scheduler": _old_scheduler()}, sort_keys=False),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("\n", encoding="utf-8")
    (tmp_path / ".aigateway-install.env").write_text(
        "AIGATEWAY_ACCELERATOR=cuda\nAIGATEWAY_PRODUCTION=false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(renderer, "discover_devices", _new_inventory)
    monkeypatch.setattr(controller, "_load_renderer", lambda _root: renderer)
    monkeypatch.setattr(
        controller.subprocess,
        "run",
        lambda command, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="",
        ),
    )

    assert controller.reconcile(tmp_path, apply=False) is True

    scheduler = yaml.safe_load(runtime_config.read_text(encoding="utf-8"))[
        "gpu_scheduler"
    ]
    assert scheduler["gateway_devices"] == ["GPU-new"]
    assert scheduler["comfyui_devices"] == ["GPU-new"]
    assert scheduler["device_overrides"][0]["uuid"] == "GPU-new"
    assert scheduler["workers"][0]["device_uuid"] == "GPU-new"
    assert scheduler["workers"][0]["logical_index"] == 1
