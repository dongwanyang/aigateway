from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_controller_records_initial_topology_then_auto_applies_uuid_change(
    tmp_path: Path, monkeypatch
) -> None:
    scripts = tmp_path / "scripts"
    runtime = tmp_path / ".aigateway" / "runtime"
    scripts.mkdir()
    runtime.mkdir(parents=True)
    renderer = _module(
        REPO_ROOT / "scripts" / "render-gpu-topology.py", "test_renderer"
    )
    controller = _module(
        REPO_ROOT / "scripts" / "gpu-topology-controller.py", "test_controller"
    )
    inventory = [
        {
            "index": 0,
            "uuid": "GPU-a",
            "name": "GPU A",
            "memory_total_mb": 16384,
        },
        {
            "index": 1,
            "uuid": "GPU-b",
            "name": "GPU B",
            "memory_total_mb": 49152,
        },
    ]
    monkeypatch.setattr(renderer, "discover_devices", lambda: inventory)
    monkeypatch.setattr(controller, "_load_renderer", lambda _root: renderer)
    (tmp_path / ".aigateway-install.env").write_text(
        "AIGATEWAY_ACCELERATOR=cuda\nAIGATEWAY_PRODUCTION=false\n",
        encoding="utf-8",
    )
    initial_compose, initial_workers = renderer.render_topology(inventory)
    config = {
        "gpu_scheduler": {
            "comfyui_devices": "auto",
            "gateway_devices": "auto",
            "device_overrides": [],
            "topology_auto_apply": True,
            "workers": initial_workers,
        }
    }
    renderer._atomic_yaml(runtime / "config.yaml", config)
    renderer._atomic_yaml(
        runtime / "docker-compose.gpu.generated.yml", initial_compose
    )

    assert controller.reconcile(tmp_path, apply=True) is False
    state_path = runtime / ".gpu-topology-controller.json"
    first_fingerprint = json.loads(state_path.read_text())["fingerprint"]

    config["gpu_scheduler"]["comfyui_devices"] = ["GPU-b"]
    renderer._atomic_yaml(runtime / "config.yaml", config)
    calls: list[list[str]] = []

    def _run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(controller.subprocess, "run", _run)
    assert controller.reconcile(tmp_path, apply=True) is True

    updated = yaml.safe_load((runtime / "config.yaml").read_text())
    assert [item["device_uuid"] for item in updated["gpu_scheduler"]["workers"]] == [
        "GPU-b"
    ]
    assert any("up" in command and "--force-recreate" in command for command in calls)
    assert json.loads(state_path.read_text())["fingerprint"] != first_fingerprint


def test_controller_retries_after_compose_apply_failure(
    tmp_path: Path, monkeypatch
) -> None:
    scripts = tmp_path / "scripts"
    runtime = tmp_path / ".aigateway" / "runtime"
    scripts.mkdir()
    runtime.mkdir(parents=True)
    renderer = _module(
        REPO_ROOT / "scripts" / "render-gpu-topology.py", "retry_renderer"
    )
    controller = _module(
        REPO_ROOT / "scripts" / "gpu-topology-controller.py", "retry_controller"
    )
    inventory = [
        {"index": 0, "uuid": "GPU-a", "name": "GPU A", "memory_total_mb": 16384}
    ]
    monkeypatch.setattr(renderer, "discover_devices", lambda: inventory)
    monkeypatch.setattr(controller, "_load_renderer", lambda _root: renderer)
    (tmp_path / ".aigateway-install.env").write_text(
        "AIGATEWAY_ACCELERATOR=cuda\nAIGATEWAY_PRODUCTION=false\n",
        encoding="utf-8",
    )
    renderer._atomic_yaml(
        runtime / "config.yaml",
        {"gpu_scheduler": {"comfyui_devices": "auto"}},
    )
    calls: list[list[str]] = []

    def _fail_apply(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0 if "config" in command else 1,
            stdout="",
            stderr="failed",
        )

    monkeypatch.setattr(controller.subprocess, "run", _fail_apply)
    try:
        controller.reconcile(tmp_path, apply=True)
    except RuntimeError as exc:
        assert "topology apply failed" in str(exc)
    else:
        raise AssertionError("expected failed Compose apply")

    state_path = runtime / ".gpu-topology-controller.json"
    pending = json.loads(state_path.read_text(encoding="utf-8"))
    assert "pending_fingerprint" in pending

    def _succeed(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(controller.subprocess, "run", _succeed)
    assert controller.reconcile(tmp_path, apply=True) is True
    assert sum("up" in command for command in calls) == 2
    applied = json.loads(state_path.read_text(encoding="utf-8"))
    assert set(applied) == {"fingerprint"}


def test_dynamic_vram_config_change_recreates_worker(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / ".aigateway" / "runtime"
    runtime.mkdir(parents=True)
    renderer = _module(
        REPO_ROOT / "scripts" / "render-gpu-topology.py", "dynamic_renderer"
    )
    controller = _module(
        REPO_ROOT / "scripts" / "gpu-topology-controller.py",
        "dynamic_controller",
    )
    inventory = [
        {"index": 0, "uuid": "GPU-a", "name": "GPU A", "memory_total_mb": 16384}
    ]
    monkeypatch.setattr(renderer, "discover_devices", lambda: inventory)
    monkeypatch.setattr(controller, "_load_renderer", lambda _root: renderer)
    (tmp_path / ".aigateway-install.env").write_text(
        "AIGATEWAY_ACCELERATOR=cuda\nAIGATEWAY_PRODUCTION=false\n",
        encoding="utf-8",
    )
    config = {
        "gpu_scheduler": {
            "comfyui_devices": "auto",
            "comfyui_dynamic_vram_enabled": False,
            "topology_auto_apply": True,
        }
    }
    initial_compose, workers = renderer.render_topology(
        inventory, config["gpu_scheduler"]
    )
    config["gpu_scheduler"]["workers"] = workers
    renderer._atomic_yaml(runtime / "config.yaml", config)
    renderer._atomic_yaml(
        runtime / "docker-compose.gpu.generated.yml", initial_compose
    )
    assert controller.reconcile(tmp_path, apply=True) is False

    config["gpu_scheduler"]["comfyui_dynamic_vram_enabled"] = True
    renderer._atomic_yaml(runtime / "config.yaml", config)
    calls: list[list[str]] = []

    def _run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(controller.subprocess, "run", _run)
    assert controller.reconcile(tmp_path, apply=True) is True

    generated = yaml.safe_load(
        (runtime / "docker-compose.gpu.generated.yml").read_text()
    )
    assert (
        generated["services"]["comfyui"]["environment"][
            "COMFYUI_DISABLE_DYNAMIC_VRAM"
        ]
        == "${COMFYUI_DISABLE_DYNAMIC_VRAM:-false}"
    )
    assert any("--force-recreate" in command for command in calls)
