#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"expected block not found in {path}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "scripts/quickstart.sh",
    '''if [[ "$accelerator" == "cuda" && "$gpu_count" == "1" \\
      && "$needs_studio" == "true" ]]; then
  shared_gpu="true"
  gateway_memory_fraction="0.20"
fi
''',
    '''if [[ "$accelerator" == "cuda" && "$gpu_count" == "1" \\
      && "$needs_studio" == "true" ]]; then
  shared_gpu="true"
  # A fractional PyTorch limit still initializes a CUDA context during Gateway
  # startup.  On a one-GPU Studio/Full installation the GPU belongs exclusively
  # to ComfyUI; Gateway helper models are rendered onto CPU and CUDA is hidden.
  gateway_gpu_device=-1
  gateway_memory_fraction=""
fi
''',
)

replace_once(
    "tests/unit/cli/test_quickstart_script.py",
    '''    assert "AIGATEWAY_SHARED_GPU=true" in state
    assert "GATEWAY_CUDA_MEMORY_FRACTION=0.20" in state
''',
    '''    assert "AIGATEWAY_SHARED_GPU=true" in state
    assert "GATEWAY_CUDA_VISIBLE_DEVICES=-1" in state
    assert "GATEWAY_CUDA_MEMORY_FRACTION=" in state
    assert "GATEWAY_CUDA_MEMORY_FRACTION=0.20" not in state
''',
)
