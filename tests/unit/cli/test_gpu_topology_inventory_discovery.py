from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "render-gpu-topology.py"


def _module():
    spec = importlib.util.spec_from_file_location(
        "gpu_topology_inventory_discovery", SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_discovery_falls_back_when_direct_inventory_is_partial(monkeypatch) -> None:
    renderer = _module()

    def _run(command, **_kwargs):
        if "--query-gpu=index,uuid,name,memory.total" in command:
            return SimpleNamespace(
                returncode=0,
                stdout="0, GPU-direct, GPU A, 16384\nmalformed\n",
                stderr="",
            )
        if command[-1] == "-L":
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "GPU 1: GPU B (UUID: GPU-b)\n"
                    "GPU 0: GPU A (UUID: GPU-a)\n"
                ),
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout="16384\n24576\n",
            stderr="",
        )

    monkeypatch.setattr(renderer.subprocess, "run", _run)

    devices = renderer.discover_devices()

    assert [item["index"] for item in devices] == [0, 1]
    assert [item["uuid"] for item in devices] == ["GPU-a", "GPU-b"]


def test_discovery_returns_empty_on_nvidia_smi_timeout(monkeypatch) -> None:
    renderer = _module()

    def _timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("nvidia-smi", 5)

    monkeypatch.setattr(renderer.subprocess, "run", _timeout)

    assert renderer.discover_devices() == []
