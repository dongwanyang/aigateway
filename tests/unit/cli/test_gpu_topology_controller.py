from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _complete_initial_topology(controller, renderer, scheduler, inventory) -> None:
    scheduler["inventory_source"] = "host_generated"
    scheduler["devices"] = renderer._runtime_inventory(inventory)
    scheduler["inventory_fingerprint"] = controller._fingerprint(
        scheduler, inventory
    )


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
    _complete_initial_topology(
        controller, renderer, config["gpu_scheduler"], inventory
    )
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
    assert [item["uuid"] for item in updated["gpu_scheduler"]["devices"]] == [
        "GPU-a",
        "GPU-b",
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
    _complete_initial_topology(
        controller, renderer, config["gpu_scheduler"], inventory
    )
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


def test_controller_preserves_concurrent_non_gpu_config_update(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / ".aigateway" / "runtime"
    runtime.mkdir(parents=True)
    renderer = _module(
        REPO_ROOT / "scripts" / "render-gpu-topology.py", "merge_renderer"
    )
    controller = _module(
        REPO_ROOT / "scripts" / "gpu-topology-controller.py", "merge_controller"
    )
    inventory = [
        {"index": 0, "uuid": "GPU-a", "name": "GPU A", "memory_total_mb": 16384}
    ]
    monkeypatch.setattr(renderer, "discover_devices", lambda: inventory)
    monkeypatch.setattr(controller, "_load_renderer", lambda _root: renderer)
    (tmp_path / ".env").write_text("CUSTOM_IMAGE=example\n", encoding="utf-8")
    (tmp_path / ".aigateway-install.env").write_text(
        "AIGATEWAY_ACCELERATOR=cuda\nAIGATEWAY_PRODUCTION=false\n",
        encoding="utf-8",
    )
    renderer._atomic_yaml(
        runtime / "config.yaml",
        {
            "providers": {"before": {"enabled": True}},
            "gpu_scheduler": {
                "comfyui_devices": "auto",
                "gateway_devices": "auto",
                "topology_auto_apply": True,
            },
        },
    )
    validation_seen = False

    def _run(command, **_kwargs):
        nonlocal validation_seen
        if "config" in command and not validation_seen:
            validation_seen = True
            latest = yaml.safe_load((runtime / "config.yaml").read_text())
            latest["providers"] = {"concurrent": {"enabled": True}}
            renderer._atomic_yaml(runtime / "config.yaml", latest)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(controller.subprocess, "run", _run)

    assert controller.reconcile(tmp_path, apply=True) is True
    updated = yaml.safe_load((runtime / "config.yaml").read_text())
    assert updated["providers"] == {"concurrent": {"enabled": True}}
    assert updated["gpu_scheduler"]["workers"][0]["device_uuid"] == "GPU-a"
    assert updated["gpu_scheduler"]["devices"][0]["uuid"] == "GPU-a"


def test_compose_command_uses_project_env_before_install_state(tmp_path: Path) -> None:
    controller = _module(
        REPO_ROOT / "scripts" / "gpu-topology-controller.py", "env_controller"
    )
    install_state = tmp_path / ".aigateway-install.env"
    generated = tmp_path / "runtime" / "gpu.yml"
    command = controller._compose_command(
        tmp_path,
        install_state,
        generated,
        {"AIGATEWAY_ACCELERATOR": "cuda"},
    )

    assert command.index(str(tmp_path / ".env")) < command.index(str(install_state))


def test_manual_reconcile_ignores_disabled_auto_watch_flag(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / ".aigateway" / "runtime"
    runtime.mkdir(parents=True)
    renderer = _module(
        REPO_ROOT / "scripts" / "render-gpu-topology.py", "manual_renderer"
    )
    controller = _module(
        REPO_ROOT / "scripts" / "gpu-topology-controller.py", "manual_controller"
    )
    inventory = [
        {"index": 0, "uuid": "GPU-a", "name": "GPU A", "memory_total_mb": 16384}
    ]
    monkeypatch.setattr(renderer, "discover_devices", lambda: inventory)
    monkeypatch.setattr(controller, "_load_renderer", lambda _root: renderer)
    monkeypatch.setattr(
        controller.subprocess,
        "run",
        lambda command, **_kwargs: SimpleNamespace(
            returncode=0, stdout="", stderr=""
        ),
    )
    (tmp_path / ".env").write_text("\n", encoding="utf-8")
    (tmp_path / ".aigateway-install.env").write_text(
        "AIGATEWAY_ACCELERATOR=cuda\nAIGATEWAY_PRODUCTION=false\n",
        encoding="utf-8",
    )
    renderer._atomic_yaml(
        runtime / "config.yaml",
        {
            "gpu_scheduler": {
                "topology_auto_apply": False,
                "gateway_devices": "auto",
                "comfyui_devices": "auto",
            }
        },
    )

    assert controller.reconcile(tmp_path, apply=False) is True


def test_controller_detects_topology_change_during_compose_apply(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / ".aigateway" / "runtime"
    runtime.mkdir(parents=True)
    renderer = _module(
        REPO_ROOT / "scripts" / "render-gpu-topology.py", "race_renderer"
    )
    controller = _module(
        REPO_ROOT / "scripts" / "gpu-topology-controller.py", "race_controller"
    )
    inventory = [
        {"index": 0, "uuid": "GPU-a", "name": "GPU A", "memory_total_mb": 16384}
    ]
    monkeypatch.setattr(renderer, "discover_devices", lambda: inventory)
    monkeypatch.setattr(controller, "_load_renderer", lambda _root: renderer)
    (tmp_path / ".env").write_text("\n", encoding="utf-8")
    (tmp_path / ".aigateway-install.env").write_text(
        "AIGATEWAY_ACCELERATOR=cuda\nAIGATEWAY_PRODUCTION=false\n",
        encoding="utf-8",
    )
    renderer._atomic_yaml(
        runtime / "config.yaml",
        {
            "gpu_scheduler": {
                "gateway_devices": "auto",
                "comfyui_devices": "auto",
                "comfyui_dynamic_vram_enabled": False,
            }
        },
    )
    calls = 0

    def _run(command, **_kwargs):
        nonlocal calls
        calls += 1
        if "up" in command:
            latest = yaml.safe_load((runtime / "config.yaml").read_text())
            latest["gpu_scheduler"]["comfyui_dynamic_vram_enabled"] = True
            renderer._atomic_yaml(runtime / "config.yaml", latest)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(controller.subprocess, "run", _run)

    with pytest.raises(RuntimeError, match="changed during apply"):
        controller.reconcile(tmp_path, apply=True)

    assert calls == 2
    state = json.loads(
        (runtime / ".gpu-topology-controller.json").read_text(encoding="utf-8")
    )
    assert set(state) == {"pending_fingerprint"}


def test_controller_replaces_devices_and_workers_from_same_uuid_inventory(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / ".aigateway" / "runtime"
    runtime.mkdir(parents=True)
    renderer = _module(
        REPO_ROOT / "scripts" / "render-gpu-topology.py", "uuid_renderer"
    )
    controller = _module(
        REPO_ROOT / "scripts" / "gpu-topology-controller.py", "uuid_controller"
    )
    inventory = [
        {
            "index": 0,
            "uuid": "GPU-old",
            "name": "Old GPU",
            "memory_total_mb": 16384,
        }
    ]
    monkeypatch.setattr(renderer, "discover_devices", lambda: inventory)
    monkeypatch.setattr(controller, "_load_renderer", lambda _root: renderer)
    monkeypatch.setattr(
        controller.subprocess,
        "run",
        lambda command, **_kwargs: SimpleNamespace(
            returncode=0, stdout="", stderr=""
        ),
    )
    (tmp_path / ".env").write_text("\n", encoding="utf-8")
    (tmp_path / ".aigateway-install.env").write_text(
        "AIGATEWAY_ACCELERATOR=cuda\nAIGATEWAY_PRODUCTION=false\n",
        encoding="utf-8",
    )
    compose, workers = renderer.render_topology(inventory)
    scheduler = {
        "gateway_devices": "auto",
        "comfyui_devices": "auto",
        "device_overrides": [],
        "topology_auto_apply": True,
        "workers": workers,
    }
    _complete_initial_topology(controller, renderer, scheduler, inventory)
    renderer._atomic_yaml(
        runtime / "config.yaml", {"gpu_scheduler": scheduler}
    )
    renderer._atomic_yaml(
        runtime / "docker-compose.gpu.generated.yml", compose
    )
    assert controller.reconcile(tmp_path, apply=True) is False

    inventory[:] = [
        {
            "index": 0,
            "uuid": "GPU-new",
            "name": "New GPU",
            "memory_total_mb": 24576,
        }
    ]
    assert controller.reconcile(tmp_path, apply=True) is True

    updated = yaml.safe_load((runtime / "config.yaml").read_text())[
        "gpu_scheduler"
    ]
    assert [item["uuid"] for item in updated["devices"]] == ["GPU-new"]
    assert [item["device_uuid"] for item in updated["workers"]] == [
        "GPU-new"
    ]
    assert [item["logical_index"] for item in updated["workers"]] == [0]
    assert {
        item["device_uuid"] for item in updated["workers"]
    }.issubset({item["uuid"] for item in updated["devices"]})
    generated = yaml.safe_load(
        (runtime / "docker-compose.gpu.generated.yml").read_text()
    )
    assert (
        generated["services"]["comfyui"]["environment"][
            "CUDA_VISIBLE_DEVICES"
        ]
        == "0"
    )