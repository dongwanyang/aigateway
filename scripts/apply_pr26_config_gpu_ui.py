"""One-shot source transformation for PR #26 Config GPU status contract."""
from __future__ import annotations

from pathlib import Path

path = Path("control-panel/src/pages/Config.tsx")
text = path.read_text(encoding="utf-8")

old_interface = """interface GatewayGpuStatusView {
  available?: boolean
  torch_initialized?: boolean
  cuda_disabled?: boolean
  allocated_bytes?: number
  reserved_bytes?: number
  error?: string | null
}

interface GenerationPresetView {
"""
new_interface = """interface GatewayGpuStatusView {
  available?: boolean
  torch_initialized?: boolean
  cuda_disabled?: boolean
  allocated_bytes?: number
  reserved_bytes?: number
  error?: string | null
}

interface GpuExecutionView {
  available?: boolean
  mode?: 'scheduler_pool' | 'gateway_pool' | 'scheduler_error' | 'gateway' | 'delegated_comfyui' | 'unavailable'
  topology_complete?: boolean
  runnable_now?: boolean
  device_count?: number
  worker_count?: number
  runnable_worker_count?: number
  error?: string | null
}

interface GenerationPresetView {
"""
if old_interface not in text:
    raise SystemExit("GatewayGpuStatusView anchor not found")
text = text.replace(old_interface, new_interface, 1)

old_state = """  const comfyStatus = comfyQuery.data as ComfyStatusView | undefined
  const gatewayStatus = gpuQuery.data?.gateway as GatewayGpuStatusView | undefined
  const comfyConfigurationErrors = stringList(comfyStatus?.configuration_errors)
"""
new_state = """  const comfyStatus = comfyQuery.data as ComfyStatusView | undefined
  const gatewayStatus = gpuQuery.data?.gateway as GatewayGpuStatusView | undefined
  const gpuExecution = gpuQuery.data?.execution as GpuExecutionView | undefined
  const comfyConfigurationErrors = stringList(comfyStatus?.configuration_errors)
"""
if old_state not in text:
    raise SystemExit("GPU state anchor not found")
text = text.replace(old_state, new_state, 1)

old_block = """              {gatewayStatus?.torch_initialized ? (
                <div className=\"text-xs\" style={{ color: 'var(--color-text-tertiary)' }}>
                  allocated {formatBytes(gatewayStatus.allocated_bytes)} · reserved {formatBytes(gatewayStatus.reserved_bytes)}
                </div>
              ) : (gpuQuery.data.scheduler?.devices?.length ?? 0) > 0 ? (
                <div role=\"status\" className=\"space-y-1\">
                  <div className=\"text-xs font-medium\" style={{ color: 'var(--color-success)' }}>
                    动态资源池可用 · {gpuQuery.data.scheduler?.devices.length ?? 0} 张 GPU
                  </div>
                  <div className=\"text-xs\" style={{ color: 'var(--color-text-tertiary)' }}>
                    Gateway 空闲时可借用 GPU；生成排队后停止新租约并等待在途推理安全结束。
                  </div>
                </div>
              ) : (gpuQuery.data.shared_gpu || gatewayStatus?.cuda_disabled) && gpuQuery.data.comfyui.available ? (
                <div role=\"status\" className=\"space-y-1\">
                  <div className=\"text-xs font-medium\" style={{ color: 'var(--color-success)' }}>GPU 已保留给 ComfyUI</div>
                  <div className=\"text-xs\" style={{ color: 'var(--color-text-tertiary)' }}>
                    Gateway 辅助模型使用 CPU，不建立 CUDA 上下文，以把显存完整留给本地图片和视频生成。
                  </div>
                </div>
              ) : gatewayStatus?.available ? (
                <div role=\"status\" className=\"space-y-1\">
                  <div className=\"text-xs font-medium\" style={{ color: 'var(--color-text-secondary)' }}>未初始化 CUDA</div>
                  <div className=\"text-xs\" style={{ color: 'var(--color-text-tertiary)' }}>
                    GPU 设备可用，但 Gateway 尚未建立 CUDA 上下文，以避免空闲占用 ComfyUI 显存。
                  </div>
                </div>
              ) : (
                <div role=\"alert\" className=\"space-y-1\">
                  <div className=\"text-xs font-medium\" style={{ color: 'var(--color-danger)' }}>GPU 状态不可用</div>
                  <div className=\"text-xs\" style={{ color: 'var(--color-text-tertiary)' }}>
                    无法通过 nvidia-smi 或 PyTorch 获取设备状态{gatewayStatus?.error ? `：${gatewayStatus.error}` : '。'}
                  </div>
                </div>
              )}
"""
new_block = """              {gpuExecution?.mode === 'scheduler_error' ? (
                <div role=\"alert\" className=\"space-y-1\">
                  <div className=\"text-xs font-medium\" style={{ color: 'var(--color-danger)' }}>GPU 资源池拓扑错误</div>
                  <div className=\"text-xs\" style={{ color: 'var(--color-text-tertiary)' }}>
                    Scheduler 已启用，但本地设备与 ComfyUI Worker 未形成有效 UUID 配对
                    {gpuExecution.error ? `：${gpuExecution.error}` : '。'}
                  </div>
                </div>
              ) : gpuExecution?.mode === 'scheduler_pool' && gpuExecution.available ? (
                <div role=\"status\" className=\"space-y-1\">
                  <div className=\"text-xs font-medium\" style={{ color: 'var(--color-success)' }}>
                    动态资源池可用 · {gpuExecution.device_count ?? gpuQuery.data.scheduler?.devices?.length ?? 0} 张 GPU
                  </div>
                  <div className=\"text-xs\" style={{ color: 'var(--color-text-tertiary)' }}>
                    Gateway 空闲时可借用 GPU；生成排队后停止新租约并等待在途推理安全结束。
                  </div>
                </div>
              ) : gpuExecution?.mode === 'scheduler_pool' ? (
                <div role=\"alert\" className=\"space-y-1\">
                  <div className=\"text-xs font-medium\" style={{ color: 'var(--color-warning)' }}>GPU 资源池暂不可调度</div>
                  <div className=\"text-xs\" style={{ color: 'var(--color-text-tertiary)' }}>
                    拓扑已建立，但当前没有健康且未处于冷却或 OOM 隔离的 Worker
                    {gpuExecution.error ? `：${gpuExecution.error}` : '。'}
                  </div>
                </div>
              ) : gpuExecution?.mode === 'gateway_pool' ? (
                <div role=\"status\" className=\"space-y-1\">
                  <div className=\"text-xs font-medium\" style={{ color: gpuExecution.available ? 'var(--color-success)' : 'var(--color-warning)' }}>
                    Gateway GPU 池{gpuExecution.available ? '可用' : '暂不可用'}
                  </div>
                  <div className=\"text-xs\" style={{ color: 'var(--color-text-tertiary)' }}>
                    本地 GPU 由 Scheduler 管理；外部 ComfyUI 不参与本机设备租约。
                  </div>
                </div>
              ) : gpuExecution?.mode === 'delegated_comfyui' ? (
                <div role=\"status\" className=\"space-y-1\">
                  <div className=\"text-xs font-medium\" style={{ color: 'var(--color-success)' }}>使用外部 ComfyUI</div>
                  <div className=\"text-xs\" style={{ color: 'var(--color-text-tertiary)' }}>
                    本地 GPU Scheduler 已禁用，生成任务直接提交到外部端点。
                  </div>
                </div>
              ) : gatewayStatus?.torch_initialized ? (
                <div className=\"text-xs\" style={{ color: 'var(--color-text-tertiary)' }}>
                  allocated {formatBytes(gatewayStatus.allocated_bytes)} · reserved {formatBytes(gatewayStatus.reserved_bytes)}
                </div>
              ) : gpuExecution?.mode === 'gateway' || gatewayStatus?.available ? (
                <div role=\"status\" className=\"space-y-1\">
                  <div className=\"text-xs font-medium\" style={{ color: 'var(--color-text-secondary)' }}>未初始化 CUDA</div>
                  <div className=\"text-xs\" style={{ color: 'var(--color-text-tertiary)' }}>
                    GPU 设备可用，但 Gateway 尚未建立 CUDA 上下文。
                  </div>
                </div>
              ) : (
                <div role=\"alert\" className=\"space-y-1\">
                  <div className=\"text-xs font-medium\" style={{ color: 'var(--color-danger)' }}>GPU 状态不可用</div>
                  <div className=\"text-xs\" style={{ color: 'var(--color-text-tertiary)' }}>
                    无法获取有效 GPU 执行状态{gpuExecution?.error || gatewayStatus?.error ? `：${gpuExecution?.error ?? gatewayStatus?.error}` : '。'}
                  </div>
                </div>
              )}
"""
if old_block not in text:
    raise SystemExit("GPU summary block not found")
text = text.replace(old_block, new_block, 1)

path.write_text(text, encoding="utf-8")
