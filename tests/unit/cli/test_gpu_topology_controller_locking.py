from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTROLLER = REPO_ROOT / "scripts" / "gpu-topology-controller.py"


def _module():
    spec = importlib.util.spec_from_file_location(
        "gpu_topology_controller_locking", CONTROLLER
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_config_write_lock_blocks_another_process_on_config_inode(
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
