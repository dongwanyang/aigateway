#!/usr/bin/env python3
from __future__ import annotations

import subprocess
from pathlib import Path

GPU_BRANCH = "agent/fix-gpu-resource-observability"


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if new in text:
        return
    if old not in text:
        raise SystemExit(f"expected integration point not found in {path}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


run("git", "fetch", "origin", GPU_BRANCH)
run(
    "git",
    "merge",
    "--no-commit",
    "--no-ff",
    "-X",
    "ours",
    f"origin/{GPU_BRANCH}",
)

# Preserve the backend component's secure exception handling while adding the
# GPU component's idle-unload configuration to the same application lifecycle.
replace_once(
    "aigateway-api/src/aigateway_api/main.py",
    '''        from aigateway_core.prefix.cache.l3_semantic import (
            set_l3_device,
            set_l3_model,
        )
''',
    '''        from aigateway_core.prefix.cache.l3_semantic import (
            set_l3_device,
            set_l3_idle_unload_seconds,
            set_l3_model,
        )
''',
)
replace_once(
    "aigateway-api/src/aigateway_api/main.py",
    '''        set_l3_model(config_manager.get("embedding.model", "Qwen/Qwen3-Embedding-0.6B"))
        set_l3_device(l3_dev)
''',
    '''        set_l3_model(config_manager.get("embedding.model", "Qwen/Qwen3-Embedding-0.6B"))
        set_l3_device(l3_dev)
        set_l3_idle_unload_seconds(
            float(config_manager.get("embedding.idle_unload_seconds", 300))
        )
''',
)

# Preserve the UI component's multi-series metrics contract if the merge chose
# the GPU side of client.ts for a conflict.
replace_once(
    "control-panel/src/api/client.ts",
    '''export interface MetricsJsonData { prometheus: Record<string, { labels: Record<string, string>; value: number }>; keys:''',
    '''export interface MetricsJsonData { prometheus: Record<string, { labels: Record<string, string>; value: number }>; prometheus_series?: Record<string, Array<{ labels: Record<string, string>; value: number }>>; keys:''',
)

unmerged = subprocess.run(
    ["git", "diff", "--name-only", "--diff-filter=U"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
if unmerged:
    raise SystemExit(f"unresolved merge paths:\n{unmerged}")

run("git", "add", "-A")
