from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_entrypoint_seeds_manager_config_once(tmp_path):
    repo = Path(__file__).resolve().parents[3]
    script = repo / "scripts" / "comfyui-entrypoint.sh"
    template = repo / "scripts" / "comfyui-manager-config.ini"
    env = {
        **os.environ,
        "COMFYUI_ROOT": str(tmp_path / "ComfyUI"),
        "COMFYUI_MANAGER_CONFIG_TEMPLATE": str(template),
        "COMFYUI_PYTHON": "/bin/true",
    }

    subprocess.run([str(script)], env=env, check=True)
    config = tmp_path / "ComfyUI" / "user" / "__manager" / "config.ini"
    assert "security_level = normal" in config.read_text(encoding="utf-8")
    assert (tmp_path / "ComfyUI" / "custom_nodes").is_dir()

    config.write_text("[default]\nsecurity_level = strong\n", encoding="utf-8")
    subprocess.run([str(script)], env=env, check=True)
    assert config.read_text(encoding="utf-8") == (
        "[default]\nsecurity_level = strong\n"
    )


def test_entrypoint_disables_dynamic_vram_by_default(tmp_path):
    repo = Path(__file__).resolve().parents[3]
    script = repo / "scripts" / "comfyui-entrypoint.sh"
    template = repo / "scripts" / "comfyui-manager-config.ini"
    env = {
        **os.environ,
        "COMFYUI_ROOT": str(tmp_path / "ComfyUI"),
        "COMFYUI_MANAGER_CONFIG_TEMPLATE": str(template),
        "COMFYUI_PYTHON": "/bin/echo",
    }

    completed = subprocess.run(
        [str(script)], env=env, check=True, capture_output=True, text=True
    )

    assert "--disable-dynamic-vram" in completed.stdout.split()


def test_entrypoint_can_explicitly_enable_dynamic_vram(tmp_path):
    repo = Path(__file__).resolve().parents[3]
    script = repo / "scripts" / "comfyui-entrypoint.sh"
    template = repo / "scripts" / "comfyui-manager-config.ini"
    env = {
        **os.environ,
        "COMFYUI_ROOT": str(tmp_path / "ComfyUI"),
        "COMFYUI_MANAGER_CONFIG_TEMPLATE": str(template),
        "COMFYUI_PYTHON": "/bin/echo",
        "COMFYUI_DISABLE_DYNAMIC_VRAM": "false",
    }

    completed = subprocess.run(
        [str(script)], env=env, check=True, capture_output=True, text=True
    )

    assert "--disable-dynamic-vram" not in completed.stdout.split()
