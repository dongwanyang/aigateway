"""Add the normalized execution contract to the control-panel API type."""
from pathlib import Path

path = Path("control-panel/src/api/_clientCore.ts")
text = path.read_text(encoding="utf-8")
old = '''export interface GpuStatusData {
  gateway: { available: boolean; name?: string | null; allocated_bytes: number; reserved_bytes: number; device_used_bytes: number; device_free_bytes: number; device_total_bytes: number }
  comfyui: { available: boolean; memory: { total_bytes: number | null; free_bytes: number | null; used_bytes: number | null } | null; endpoint_errors?: Record<string, string> }
'''
new = '''export interface GpuStatusData {
  gateway: { available: boolean; name?: string | null; allocated_bytes: number; reserved_bytes: number; device_used_bytes: number; device_free_bytes: number; device_total_bytes: number }
  execution?: {
    available: boolean
    mode: 'scheduler_pool' | 'gateway_pool' | 'scheduler_error' | 'gateway' | 'delegated_comfyui' | 'unavailable'
    owner: 'scheduler' | 'gateway' | 'comfyui' | null
    topology_complete: boolean
    runnable_now: boolean
    device_count?: number
    worker_count?: number
    runnable_device_count?: number
    runnable_worker_count?: number
    supported_capabilities?: string[]
    external_comfyui_available?: boolean
    memory?: { total_bytes?: number | null; free_bytes?: number | null; used_bytes?: number | null } | null
    error?: string
  }
  comfyui: { available: boolean; memory: { total_bytes: number | null; free_bytes: number | null; used_bytes: number | null } | null; endpoint_errors?: Record<string, string> }
'''
if old not in text:
    raise SystemExit("GpuStatusData anchor not found")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
