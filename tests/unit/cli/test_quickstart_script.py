"""Behavior tests for the unified quickstart installer without invoking Docker."""

from __future__ import annotations

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/unit/cli/ → aigateway/ (parents[3])


def _run_quickstart(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_quickstart_persists_full_profile_and_supports_incremental_changes(tmp_path):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script = scripts_dir / "quickstart.sh"
    script.write_text(
        (REPO_ROOT / "scripts" / "quickstart.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    _run_quickstart(
        script,
        "--non-interactive",
        "--profile",
        "full",
        "--accelerator",
        "cuda",
        "--monitoring",
        "--no-start",
    )
    state_path = tmp_path / ".aigateway-install.env"
    state = state_path.read_text(encoding="utf-8")
    assert "GATEWAY_INSTALL_PROFILE=full" in state
    assert "GATEWAY_IMAGE_TARGET=gateway-full" in state
    assert "GATEWAY_ACCELERATOR=cuda" in state
    assert "GATEWAY_MONITORING=true" in state

    _run_quickstart(
        script,
        "--non-interactive",
        "--remove",
        "vision",
        "--no-start",
    )
    changed = state_path.read_text(encoding="utf-8")
    assert "GATEWAY_INSTALL_PROFILE=rag" in changed
    assert "GATEWAY_IMAGE_TARGET=gateway-rag" in changed
    assert "GATEWAY_ACCELERATOR=cuda" in changed


def test_quickstart_show_plan_does_not_create_state(tmp_path):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script = scripts_dir / "quickstart.sh"
    script.write_text(
        (REPO_ROOT / "scripts" / "quickstart.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    result = _run_quickstart(script, "--show-plan")
    assert "Profile    : runtime" in result.stdout
    assert not (tmp_path / ".aigateway-install.env").exists()
