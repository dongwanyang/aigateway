#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"expected block not found in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# release_l3_model can run inside asyncio.to_thread(). Task.cancel() is not
# thread-safe and the idle callback would otherwise cancel the task awaiting it.
replace_once(
    "aigateway-core/src/aigateway_core/prefix/cache/l3_semantic.py",
    '''    task = _l3_idle_task
    _l3_idle_task = None
    if task is not None and not task.done():
        task.cancel()

    with _l3_model_lock:
''',
    '''    # Incrementing the generation invalidates a pending idle timer.  Do not
    # call Task.cancel() here: this function can run in a worker thread and may
    # also have been invoked by the timer task itself.
    _l3_idle_task = None

    with _l3_model_lock:
''',
)

replace_once(
    "control-panel/src/api/client.ts",
    '''export async function releaseGpuMemory(): Promise<ApiResponse<{ gateway_models: Record<string, boolean>; comfyui: Record<string, unknown>; gateway: GpuStatusData['gateway'] }>> { return fetchJson('/admin/gpu/release', { method: 'POST', body: JSON.stringify({}) }) }
''',
    '''export async function releaseGpuMemory(): Promise<ApiResponse<{ gateway_models: Record<string, boolean>; comfyui: Record<string, unknown>; gateway: GpuStatusData['gateway'] }>> { return fetchJson<{ gateway_models: Record<string, boolean>; comfyui: Record<string, unknown>; gateway: GpuStatusData['gateway'] }>('/admin/gpu/release', { method: 'POST', body: JSON.stringify({}) }) }
''',
)

# The broad page integration suite uses the real API client with a mocked fetch.
# Add GPU endpoints before the generic /admin/config handler.
replace_once(
    "control-panel/src/pages/pages.integration.test.tsx",
    '''  if (url.includes('/admin/config/debug')) {
''',
    '''  if (url.endsWith('/admin/gpu/status')) {
    return Response.json({
      data: {
        gateway: {
          available: true,
          name: 'Test GPU',
          allocated_bytes: 1024,
          reserved_bytes: 2048,
          device_used_bytes: 4096,
          device_free_bytes: 4096,
          device_total_bytes: 8192,
        },
        comfyui: {
          available: true,
          memory: { total_bytes: 8192, free_bytes: 4096, used_bytes: 4096 },
          endpoint_errors: {},
        },
        queue: { running: 0, pending: 0 },
        queue_idle: true,
        shared_gpu: true,
        diagnosis: ['gateway_and_comfyui_share_one_gpu'],
      },
      message: 'success',
    })
  }
  if (url.endsWith('/admin/gpu/release')) {
    return Response.json({
      data: {
        gateway_models: { l3_embedding: true, rag_embedding: false },
        comfyui: { requested: true, released: true },
        gateway: {
          available: true,
          allocated_bytes: 0,
          reserved_bytes: 0,
          device_used_bytes: 0,
          device_free_bytes: 8192,
          device_total_bytes: 8192,
        },
      },
      message: 'success',
    })
  }
  if (url.includes('/admin/config/debug')) {
''',
)

# Make the isolated GPU UI test restore globals even if the assertion path changes.
gpu_test = Path("control-panel/src/pages/GpuStatus.regression.test.tsx")
text = gpu_test.read_text(encoding="utf-8")
text = text.replace(
    "import { expect, it, vi } from 'vitest'",
    "import { afterEach, expect, it, vi } from 'vitest'",
)
text = text.replace(
    "it('explains resident memory and releases it only while the queue is idle', async () => {",
    "afterEach(() => { vi.unstubAllGlobals() })\n\nit('explains resident memory and releases it only while the queue is idle', async () => {",
)
text = text.replace("  vi.unstubAllGlobals()\n})", "})")
gpu_test.write_text(text, encoding="utf-8")
