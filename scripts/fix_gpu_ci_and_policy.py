#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"expected block not found in {path}: {old[:160]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "scripts/quickstart.sh",
    '''  if [[ "$edition" != "lite" ]]       && command -v nvidia-smi >/dev/null 2>&1       && nvidia-smi -L >/dev/null 2>&1; then
    accelerator="cuda"
''',
    '''  if [[ "$edition" != "lite" ]] \\
      && command -v nvidia-smi >/dev/null 2>&1 \\
      && nvidia-smi -L >/dev/null 2>&1; then
    accelerator="cuda"
''',
)
replace_once(
    "scripts/quickstart.sh",
    '''if [[ "$accelerator" == "cuda" && "$gpu_count" == "1"       && "$needs_knowledge" == "true" && "$needs_studio" == "true" ]]; then
  shared_gpu="true"
  gateway_memory_fraction="0.20"
fi
''',
    '''if [[ "$accelerator" == "cuda" && "$gpu_count" == "1" \\
      && "$needs_studio" == "true" ]]; then
  shared_gpu="true"
  gateway_memory_fraction="0.20"
fi
''',
)
replace_once(
    "scripts/quickstart.sh",
    '''if [[ "$needs_studio" == "true" && "$platform" != "apple"       && "$accelerator" != "cuda" && "$comfyui_mode" == "container" ]]; then
  fail "本机未检测到可用 NVIDIA GPU，不能启动容器版 ComfyUI；请使用 --comfyui remote --comfyui-url URL"
fi
''',
    '''if [[ "$start" == "true" && "$needs_studio" == "true" \\
      && "$platform" != "apple" && "$accelerator" != "cuda" \\
      && "$comfyui_mode" == "container" ]]; then
  fail "本机未检测到可用 NVIDIA GPU，不能启动容器版 ComfyUI；请使用 --comfyui remote --comfyui-url URL"
fi
''',
)

replace_once(
    "control-panel/src/pages/Config.tsx",
    '''          <button
            className="btn btn-secondary"
            disabled={gpuQuery.data?.queue_idle !== true || releaseGpuMutation.isPending}
            onClick={() => releaseGpuMutation.mutate()}
            title={gpuQuery.data?.queue_idle === true ? '卸载空闲模型并清理 PyTorch 缓存' : '队列非空时不能释放显存'}
          >
''',
    '''          <button
            className="btn btn-secondary"
            disabled={!gpuQuery.data || gpuQuery.data.queue_idle === false || releaseGpuMutation.isPending}
            onClick={() => releaseGpuMutation.mutate()}
            title={gpuQuery.data?.queue_idle === false ? '队列非空时不能释放显存' : '卸载空闲模型并清理 PyTorch 缓存'}
          >
''',
)
