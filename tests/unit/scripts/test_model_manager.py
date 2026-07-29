from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_list_reports_every_missing_approved_model(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    env["AIGATEWAY_MODEL_DIR"] = str(tmp_path / "models")
    env["AIGATEWAY_COMFY_DATA_DIR"] = str(tmp_path / "comfyui")

    result = subprocess.run(
        ["bash", str(repo_root / "scripts/model-manager.sh"), "list"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout.count("missing") == 9
    assert "sdxl-base" in result.stdout
    assert "qwen-image-diffusion" in result.stdout
    assert "realesrgan-x4plus" in result.stdout
    assert "qwen3-embedding-0.6b" in result.stdout
    assert "9 个模型未安装" in result.stderr
