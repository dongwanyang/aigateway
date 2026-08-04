"""Regression coverage for runtime config ownership across quickstart runs."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def _fixture_repo(tmp_path: Path) -> Path:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in (
        "quickstart.sh",
        "render-deployment-config.py",
        "render-gpu-topology.py",
    ):
        shutil.copy2(REPO_ROOT / "scripts" / name, scripts / name)
    shutil.copy2(REPO_ROOT / "config.yaml", tmp_path / "config.yaml")
    shutil.copy2(REPO_ROOT / ".env.example", tmp_path / ".env.example")
    return scripts / "quickstart.sh"


def _run(
    script: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def _without_nvidia(tmp_path: Path) -> str:
    fake_bin = tmp_path / "no-nvidia"
    fake_bin.mkdir(exist_ok=True)
    nvidia_smi = fake_bin / "nvidia-smi"
    nvidia_smi.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    nvidia_smi.chmod(0o755)
    return f"{fake_bin}:/usr/bin:/bin"


def _with_test_gpu(tmp_path: Path) -> str:
    fake_bin = tmp_path / "test-gpu"
    fake_bin.mkdir(exist_ok=True)
    nvidia_smi = fake_bin / "nvidia-smi"
    nvidia_smi.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-L" ]]; then
  echo 'GPU 0: Test GPU (UUID: GPU-test)'
  exit 0
fi
if [[ " $* " == *" --query-gpu=memory.total "* ]]; then
  echo '16384'
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    nvidia_smi.chmod(0o755)
    return f"{fake_bin}:/usr/bin:/bin"


def test_quickstart_preserves_console_model_deletion_until_explicit_reset(
    tmp_path: Path,
) -> None:
    script = _fixture_repo(tmp_path)
    no_nvidia = {"PATH": _without_nvidia(tmp_path)}
    args = (
        "--non-interactive",
        "--edition",
        "lite",
        "--distribution",
        "source",
        "--no-start",
    )
    _run(script, *args, env=no_nvidia)

    runtime_path = tmp_path / ".aigateway/runtime/config.yaml"
    runtime = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    del runtime["providers"]["glm5.2"]
    runtime["task_routing"]["model_preferences"]["reasoning"] = [
        model
        for model in runtime["task_routing"]["model_preferences"]["reasoning"]
        if model != "glm-5.2"
    ]
    runtime["generation_optimization"]["model_router"][
        "model_capabilities"
    ].pop("glm-5.2", None)
    runtime["server"]["request_timeout_seconds"] = 321
    runtime_path.write_text(
        yaml.safe_dump(runtime, sort_keys=False),
        encoding="utf-8",
    )

    second = _run(
        script,
        "--non-interactive",
        "--no-start",
        env=no_nvidia,
    )
    assert "保留现有运行配置" in second.stdout
    persisted = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    assert "glm5.2" not in persisted["providers"]
    assert "glm-5.2" not in persisted["task_routing"]["model_preferences"]["reasoning"]
    assert "glm-5.2" not in persisted["generation_optimization"]["model_router"]["model_capabilities"]
    assert persisted["server"]["request_timeout_seconds"] == 321

    reset = _run(
        script,
        "--non-interactive",
        "--reset-config",
        "--no-start",
        env=no_nvidia,
    )
    assert "丢弃现有运行配置" in reset.stderr
    restored = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    assert "glm5.2" in restored["providers"]
    assert "glm-5.2" in restored["task_routing"]["model_preferences"]["reasoning"]
    assert restored["server"]["request_timeout_seconds"] == 120


def test_quickstart_generates_and_clears_host_gpu_inventory(tmp_path: Path) -> None:
    script = _fixture_repo(tmp_path)
    _run(
        script,
        "--non-interactive",
        "--edition",
        "studio",
        "--distribution",
        "source",
        "--no-start",
        env={"PATH": _with_test_gpu(tmp_path)},
    )

    runtime_path = tmp_path / ".aigateway/runtime/config.yaml"
    runtime = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    scheduler = runtime["gpu_scheduler"]
    assert scheduler["inventory_source"] == "host_generated"
    assert scheduler["devices"] == [
        {
            "index": 0,
            "uuid": "GPU-test",
            "name": "Test GPU",
            "total_memory_gb": 16.0,
            "free_memory_gb": 16.0,
        }
    ]
    assert scheduler["workers"][0]["device_uuid"] == "GPU-test"

    _run(
        script,
        "--non-interactive",
        "--edition",
        "lite",
        "--no-start",
        env={"PATH": _without_nvidia(tmp_path)},
    )
    downgraded = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    downgraded_scheduler = downgraded["gpu_scheduler"]
    assert "inventory_source" not in downgraded_scheduler
    assert "devices" not in downgraded_scheduler
    assert "workers" not in downgraded_scheduler
