from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "render-deployment-config.py"


def _module():
    spec = importlib.util.spec_from_file_location(
        "deployment_config_locking", SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deployment_config_lock_blocks_on_shared_config_inode(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("gpu_scheduler: {}\n", encoding="utf-8")
    contender = """
import fcntl
import sys

with open(sys.argv[1], "r+", encoding="utf-8") as handle:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(23)
raise SystemExit(0)
"""

    with _module()._config_write_lock(config_path):
        completed = subprocess.run(
            [sys.executable, "-c", contender, str(config_path)],
            capture_output=True,
            text=True,
            check=False,
        )

    assert completed.returncode == 23


def test_locked_deployment_write_preserves_config_inode(tmp_path: Path) -> None:
    module = _module()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("gpu_scheduler: {}\n", encoding="utf-8")
    inode = config_path.stat().st_ino

    with module._config_write_lock(config_path) as handle:
        assert handle is not None
        module._write_locked_yaml(
            handle,
            {"gpu_scheduler": {"enabled": True}},
        )

    assert config_path.stat().st_ino == inode
    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == {
        "gpu_scheduler": {"enabled": True}
    }
