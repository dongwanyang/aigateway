import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Config from './Config'

const api = vi.hoisted(() => ({
  getComfyUIStatus: vi.fn(),
  getGenerationPresets: vi.fn(),
  getGpuStatus: vi.fn(),
  releaseGpuMemory: vi.fn(),
}))

vi.mock('@/api/client', () => api)

function gpuResponse(execution: Record<string, unknown>) {
  return {
    data: {
      gateway: {
        available: false,
        torch_initialized: false,
        cuda_disabled: false,
        error: execution.error ?? null,
      },
      execution,
      comfyui: {
        available: true,
        memory: { total_bytes: 16_000, free_bytes: 12_000, used_bytes: 4_000 },
      },
      queue: { running: 0, pending: 0 },
      queue_idle: true,
      shared_gpu: true,
      scheduler: {
        enabled: true,
        policy: 'auto',
        generation_queue_depth: 0,
        devices: [{
          uuid: 'GPU-a',
          logical_index: 0,
          name: 'GPU',
          total_memory_gb: 16,
          free_memory_gb: 12,
          state: 'available',
          gateway_leases: 0,
          resident_components: [],
          worker_id: 'worker-a',
          queue: { running: 0, pending: 0 },
          cooldown_remaining_seconds: 0,
          oom_quarantine_remaining_seconds: 0,
        }],
        workers: [],
      },
    },
    message: 'success',
  }
}

function renderConfig() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <Config />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/admin/config/schema')) {
      return Response.json({ data: { items: [] }, message: 'success' })
    }
    if (url.endsWith('/admin/config')) {
      return Response.json({ data: { server: { port: 8000 } }, message: 'success', revision: 'r1' })
    }
    throw new Error(`unexpected request: ${url}`)
  }))
  api.getComfyUIStatus.mockResolvedValue({
    data: { available: true, public_url: '', manager_url: '', queue: { running: 0, pending: 0 }, configuration_errors: [] },
    message: 'success',
  })
  api.getGenerationPresets.mockResolvedValue({ data: [], message: 'success' })
  api.releaseGpuMemory.mockResolvedValue({ data: {}, message: 'success' })
})

afterEach(() => {
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

it('renders a scheduler topology error instead of a green pool state', async () => {
  api.getGpuStatus.mockResolvedValue(gpuResponse({
    available: false,
    mode: 'scheduler_error',
    owner: 'scheduler',
    topology_complete: false,
    runnable_now: false,
    device_count: 1,
    worker_count: 0,
    error: 'gpu_scheduler_topology_incomplete',
  }))

  renderConfig()

  expect(await screen.findByText('GPU 资源池拓扑错误')).toBeInTheDocument()
  expect(screen.queryByText(/动态资源池可用/)).not.toBeInTheDocument()
})

it('renders a structurally complete but non-runnable pool as degraded', async () => {
  api.getGpuStatus.mockResolvedValue(gpuResponse({
    available: false,
    mode: 'scheduler_pool',
    owner: 'scheduler',
    topology_complete: true,
    runnable_now: false,
    device_count: 1,
    worker_count: 1,
    runnable_worker_count: 0,
    error: 'gpu_scheduler_no_runnable_worker',
  }))

  renderConfig()

  expect(await screen.findByText('GPU 资源池暂不可调度')).toBeInTheDocument()
  expect(screen.queryByText(/动态资源池可用/)).not.toBeInTheDocument()
})

it('renders a healthy shared pool using the execution contract', async () => {
  api.getGpuStatus.mockResolvedValue(gpuResponse({
    available: true,
    mode: 'scheduler_pool',
    owner: 'scheduler',
    topology_complete: true,
    runnable_now: true,
    device_count: 1,
    worker_count: 1,
    runnable_worker_count: 1,
  }))

  renderConfig()

  expect(await screen.findByText(/动态资源池可用 · 1 张 GPU/)).toBeInTheDocument()
  expect(screen.queryByText('GPU 已保留给 ComfyUI')).not.toBeInTheDocument()
})
