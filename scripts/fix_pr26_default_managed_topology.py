"""Mark the repository's local ComfyUI defaults as scheduler-managed."""
from pathlib import Path

for filename in ("config.yaml", "config.yaml.template"):
    path = Path(filename)
    text = path.read_text(encoding="utf-8")
    old = "      scheduler_managed: false"
    new = "      scheduler_managed: true"
    if old not in text:
        raise SystemExit(f"managed topology anchor not found in {filename}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
