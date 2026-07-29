"""Behavior tests for the edition-based installer without invoking Docker."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def _fixture_repo(tmp_path: Path) -> Path:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("quickstart.sh", "render-deployment-config.py"):
        shutil.copy2(REPO_ROOT / "scripts" / name, scripts / name)
    shutil.copy2(REPO_ROOT / "config.yaml", tmp_path / "config.yaml")
    shutil.copy2(REPO_ROOT / ".env.example", tmp_path / ".env.example")
    return scripts / "quickstart.sh"


def _run(
    script: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def test_quickstart_generates_full_source_state_and_runtime_config(tmp_path):
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
    )

    state = (tmp_path / ".aigateway-install.env").read_text(encoding="utf-8")
    assert "AIGATEWAY_EDITION=full" in state
    assert "AIGATEWAY_DISTRIBUTION=source" in state
    assert "GATEWAY_IMAGE_TARGET=gateway-full" in state
    assert (
        "GATEWAY_BUILD_CACHE_FROM="
        "type=registry,ref=ghcr.io/dongwanyang/aigateway-gateway:latest-full-cuda"
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

    runtime = yaml.safe_load(
        (tmp_path / ".aigateway/runtime/config.yaml").read_text(encoding="utf-8")
    )
    assert runtime["deployment"]["edition"] == "full"
    rag = next(item for item in runtime["plugins"] if item["name"] == "rag_retriever")
    assert rag["enabled"] is True
    assert rag["config"]["embedding_device"] == "cuda"
    assert rag["config"]["rerank_device"] == "cuda"
    assert runtime["generation_optimization"]["draft_workflow"]["enabled"] is True


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
