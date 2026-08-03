"""Fix the render() keyword-only parameter ordering introduced in PR #26."""
from pathlib import Path

path = Path("scripts/render-deployment-config.py")
text = path.read_text(encoding="utf-8")
old = '''    embedding_mode: str,
    comfyui_mode: str = "remote",
    comfyui_url: str,
    embedding_url: str,
    monitoring: bool,
    shared_gpu: bool = False,
'''
new = '''    embedding_mode: str,
    comfyui_url: str,
    embedding_url: str,
    monitoring: bool,
    comfyui_mode: str = "remote",
    shared_gpu: bool = False,
'''
if old not in text:
    raise SystemExit("render signature anchor not found")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
