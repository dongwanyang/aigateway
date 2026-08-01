"""Behavior tests for the edition-based installer without invoking Docker."""

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
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        check=check,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def test_quickstart_generates_full_cpu_source_state_without_nvidia(tmp_path):
    script = _fixture_repo(tmp_path)
    _run(
        script,
        "--non-interactive",
        "--edition",
        "full",
        "--distribution",
        "source",
        "--monitoring",
        "--no-start",
        env={"PATH": "/usr/bin:/bin"},
    )

    state = (tmp_path / ".aigateway-install.env").read_text(encoding="utf-8")
    assert "AIGATEWAY_EDITION=full" in state
    assert "AIGATEWAY_DISTRIBUTION=source" in state
    assert "GATEWAY_IMAGE_TARGET=gateway-full-cpu" in state
    assert (
        "GATEWAY_BUILD_CACHE_FROM="
        "type=registry,ref=ghcr.io/dongwanyang/aigateway-gateway:latest-full-cpu"
    ) in state
    assert (
        "COMFYUI_BUILD_CACHE_FROM="
        "type=registry,ref=ghcr.io/dongwanyang/aigateway-comfyui:latest-cuda"
    ) in state
    assert (
        "CONTROL_PANEL_BUILD_CACHE_FROM="
        "type=registry,ref=ghcr.io/dongwanyang/aigateway-control-panel:latest"
    ) in state
    assert "COMPOSE_PROFILES=knowledge,comfy-container,monitoring" in state
    assert "AIGATEWAY_SHARED_GPU=false" in state

    runtime = yaml.safe_load(
        (tmp_path / ".aigateway/runtime/config.yaml").read_text(encoding="utf-8")
    )
    assert runtime["deployment"]["edition"] == "full"
    assert runtime["deployment"]["accelerator"] == "cpu"
    rag = next(item for item in runtime["plugins"] if item["name"] == "rag_retriever")
    assert rag["enabled"] is True
    assert rag["config"]["embedding_device"] == "cpu"
    assert rag["config"]["rerank_device"] == "cpu"
    assert runtime["generation_optimization"]["draft_workflow"]["enabled"] is True


def test_quickstart_refuses_to_start_local_comfyui_without_nvidia(tmp_path):
    script = _fixture_repo(tmp_path)
    result = _run(
        script,
        "--non-interactive",
        "--edition",
        "studio",
        "--distribution",
        "source",
        check=False,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode != 0
    assert "未检测到可用 NVIDIA GPU" in result.stderr
    assert "--comfyui remote" in result.stderr


def test_quickstart_marks_single_gpu_studio_as_shared(tmp_path):
    script = _fixture_repo(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    nvidia_smi = fake_bin / "nvidia-smi"
    nvidia_smi.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-L" ]]; then
  echo 'GPU 0: Test GPU (UUID: GPU-test)'
  exit 0
fi
if [[ " $* " == *" --query-gpu=memory.total "* ]]; then
  echo '24576'
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    nvidia_smi.chmod(0o755)

    _run(
        script,
        "--non-interactive",
        "--edition",
        "studio",
        "--distribution",
        "source",
        "--no-start",
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
    )

    state = (tmp_path / ".aigateway-install.env").read_text(encoding="utf-8")
    assert "GATEWAY_IMAGE_TARGET=gateway-vision" in state
    assert "AIGATEWAY_SHARED_GPU=true" in state
    assert "GATEWAY_CUDA_VISIBLE_DEVICES=all" in state
    assert "GATEWAY_CUDA_MEMORY_FRACTION=" in state
    assert "GATEWAY_CUDA_MEMORY_FRACTION=0.20" not in state

    runtime = yaml.safe_load(
        (tmp_path / ".aigateway/runtime/config.yaml").read_text(encoding="utf-8")
    )
    assert runtime["deployment"]["shared_gpu"] is True
    clip = runtime["generation_optimization"]["token_compressor"]["clip"]
    assert clip["device"] == "auto"
    assert runtime["gpu_scheduler"]["comfyui_dynamic_vram_enabled"] is False
    assert runtime["gpu_scheduler"]["workers"][0]["device_uuid"] == "GPU-test"
    assert (tmp_path / ".aigateway/runtime/docker-compose.gpu.generated.yml").exists()


def test_quickstart_migrates_legacy_single_gpu_full_state(tmp_path):
    script = _fixture_repo(tmp_path)
    (tmp_path / ".aigateway-install.env").write_text(
        "AIGATEWAY_EDITION=full\n"
        "AIGATEWAY_DISTRIBUTION=source\n"
        "AIGATEWAY_COMFYUI_MODE=container\n"
        "AIGATEWAY_EMBEDDING_MODE=container\n"
        "GATEWAY_CUDA_VISIBLE_DEVICES=0\n"
        "COMFYUI_CUDA_VISIBLE_DEVICES=0\n"
        "GATEWAY_CUDA_MEMORY_FRACTION=0.40\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "fake-bin-legacy"
    fake_bin.mkdir()
    nvidia_smi = fake_bin / "nvidia-smi"
    nvidia_smi.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-L" ]]; then
  echo 'GPU 0: Test GPU (UUID: GPU-test)'
  exit 0
fi
if [[ " $* " == *" --query-gpu=memory.total "* ]]; then
  echo '24576'
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    nvidia_smi.chmod(0o755)

    _run(
        script,
        "--non-interactive",
        "--no-start",
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
    )

    state = (tmp_path / ".aigateway-install.env").read_text(encoding="utf-8")
    assert "AIGATEWAY_SHARED_GPU=true" in state
    assert "GATEWAY_CUDA_VISIBLE_DEVICES=all" in state
    assert "GATEWAY_CUDA_MEMORY_FRACTION=\n" in state
    runtime = yaml.safe_load(
        (tmp_path / ".aigateway/runtime/config.yaml").read_text(encoding="utf-8")
    )
    assert runtime["deployment"]["shared_gpu"] is True
    assert runtime["embedding"]["device"] == "auto"


def test_quickstart_show_plan_does_not_create_state(tmp_path):
    script = _fixture_repo(tmp_path)
    result = _run(script, "--edition", "lite", "--show-plan")
    assert "Edition      : lite" in result.stdout
    assert not (tmp_path / ".aigateway-install.env").exists()


def test_quickstart_configures_remote_embedding_and_reranker(tmp_path):
    script = _fixture_repo(tmp_path)
    _run(
        script,
        "--non-interactive",
        "--edition",
        "knowledge",
        "--embedding",
        "remote",
        "--embedding-url",
        "https://embedding.example/v1",
        "--no-start",
    )

    runtime = yaml.safe_load(
        (tmp_path / ".aigateway/runtime/config.yaml").read_text(encoding="utf-8")
    )
    rag = next(item for item in runtime["plugins"] if item["name"] == "rag_retriever")
    assert rag["config"]["embedding_backend"] == "openai"
    assert rag["config"]["rerank_backend"] == "remote"
    assert rag["config"]["rerank_device"] == "remote"
    assert rag["config"]["rerank_api_base"] == "https://embedding.example/v1"


def test_quickstart_rejects_removed_profile_interface(tmp_path):
    script = _fixture_repo(tmp_path)
    result = _run(
        script,
        "--profile",
        "full",
        "--no-start",
        check=False,
    )
    assert result.returncode != 0
    assert "--edition" in result.stderr

def test_quickstart_keeps_gateway_cuda_for_remote_comfyui(tmp_path):
    script = _fixture_repo(tmp_path)
    fake_bin = tmp_path / "fake-bin-remote"
    fake_bin.mkdir()
    nvidia_smi = fake_bin / "nvidia-smi"
    nvidia_smi.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-L" ]]; then
  echo 'GPU 0: Test GPU (UUID: GPU-test)'
  exit 0
fi
if [[ " $* " == *" --query-gpu=memory.total "* ]]; then
  echo '24576'
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    nvidia_smi.chmod(0o755)

    _run(
        script,
        "--non-interactive",
        "--edition",
        "full",
        "--distribution",
        "source",
        "--comfyui",
        "remote",
        "--comfyui-url",
        "https://comfy.example.test",
        "--no-start",
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
    )

    state = (tmp_path / ".aigateway-install.env").read_text(encoding="utf-8")
    assert "AIGATEWAY_SHARED_GPU=false" in state
    assert "GATEWAY_CUDA_VISIBLE_DEVICES=all" in state
    assert "GATEWAY_CUDA_MEMORY_FRACTION=\n" in state

    runtime = yaml.safe_load(
        (tmp_path / ".aigateway/runtime/config.yaml").read_text(encoding="utf-8")
    )
    assert runtime["deployment"]["shared_gpu"] is False
    assert runtime["embedding"]["device"] == "auto"
    clip = runtime["generation_optimization"]["token_compressor"]["clip"]
    assert clip["device"] == "auto"

def test_compose_loads_project_env_before_generated_state() -> None:
    script = (REPO_ROOT / "scripts/quickstart.sh").read_text(encoding="utf-8")
    project_env = '--env-file "$ROOT_DIR/.env"'
    state_env = '--env-file "$STATE_FILE"'

    assert project_env in script
    assert state_env in script
    assert script.index(project_env) < script.index(state_env)
