import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Config from './Config'

const state = vi.hoisted(() => ({
  sharedGpu: false,
  execution: undefined as Record<string, unknown> | undefined,
  gateway: {
    available: true,
    torch_initialized: false,
    allocated_bytes: 0,
    reserved_bytes: 0,
    device_used_bytes: 500,
    device_free_bytes: 15_500,
    device_total_bytes: 16_000,
    error: null as string | null,
  },
}))

const api = vi.hoisted(() => ({
  getComfyUIStatus: vi.fn(async () => ({ data: { available: true, public_url: '', manager_url: '', queue: { running: 0, pending: 0 }, configuration_errors: [] }, message: 'success' })),
  getGenerationPresets: vi.fn(async () => ({ data: [], message: 'success' })),
  getGpuStatus: vi.fn(async () => ({
    data: {
      gateway: { ...state.gateway },
      execution: state.execution ? { ...state.execution } : undefined,
      comfyui: { available: true, memory: { total_bytes: 16_000, free_bytes: 15_500, used_bytes: 500 } },
      queue: { running: 0, pending: 0 },
      queue_idle: true,
      shared_gpu: state.sharedGpu,
      scheduler: {
        enabled: Boolean(state.execution),
        policy: 'auto',
        generation_queue_depth: 0,
        devices: state.execution ? [{
          uuid: 'GPU-a',
          logical_index: 0,
          name: 'GPU',
          total_memory_gb: 16,
          free_memory_gb: 15.5,
          state: 'available',
          gateway_leases: 0,
          resident_components: [],
          worker_id: 'worker-a',
          queue: { running: 0, pending: 0 },
          cooldown_remaining_seconds: 0,
          oom_quarantine_remaining_seconds: 0,
        }] : [],
        workers: [],
      },
      diagnosis: [],
    },
    message: 'success',
  })),
  releaseGpuMemory: vi.fn(async () => ({ data: {}, message: 'success' })),
}))
vi.mock('@/api/client', () => api)

function renderConfig() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><Config /></QueryClientProvider>)
}

beforeEach(() => {
  state.sharedGpu = false
  state.execution = undefined
  state.gateway = {
    available: true,
    torch_initialized: false,
    allocated_bytes: 0,
    reserved_bytes: 0,
    device_used_bytes: 500,
    device_free_bytes: 15_500,
    device_total_bytes: 16_000,
    error: null,
  }
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/admin/config/schema')) return Response.json({ data: { items: [] }, message: 'success' })
    if (url.endsWith('/admin/config')) return Response.json({ data: { server: { port: 8000 } }, message: 'success', revision: 'r1' })
    throw new Error(`unexpected request: ${url}`)
  }))
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it('shows an uninitialized CUDA context when the GPU device is healthy', async () => {
  renderConfig()
  expect(await screen.findByText('未初始化 CUDA')).toBeInTheDocument()
  expect(screen.getByText(/GPU 设备可用/)).toBeInTheDocument()
  expect(screen.queryByText('GPU 状态不可用')).not.toBeInTheDocument()
})

it('shows a real GPU status failure separately from an uninitialized context', async () => {
  state.sharedGpu = false
  state.gateway.available = false
  state.gateway.error = 'gpu_status_unavailable'
  renderConfig()
  expect(await screen.findByText('GPU 状态不可用')).toBeInTheDocument()
  expect(screen.getByText(/gpu_status_unavailable/)).toBeInTheDocument()
  expect(screen.queryByText('未初始化 CUDA')).not.toBeInTheDocument()
})

it('shows allocator counters after Torch initializes CUDA', async () => {
  state.gateway.torch_initialized = true
  state.gateway.allocated_bytes = 1024
  state.gateway.reserved_bytes = 2048
  renderConfig()
  expect(await screen.findByText(/allocated 1 KiB · reserved 2 KiB/)).toBeInTheDocument()
  expect(screen.queryByText('未初始化 CUDA')).not.toBeInTheDocument()
})

it('explains that Gateway and ComfyUI share a scheduler-owned pool', async () => {
  state.sharedGpu = true
  state.gateway.available = false
  state.gateway.error = 'gpu_status_unavailable'
  state.execution = {
    available: true,
    mode: 'scheduler_pool',
    owner: 'scheduler',
    topology_complete: true,
    runnable_now: true,
    device_count: 1,
    worker_count: 1,
    runnable_worker_count: 1,
  }
  renderConfig()
  expect(await screen.findByText(/动态资源池可用 · 1 张 GPU/)).toBeInTheDocument()
  expect(screen.getByText(/Gateway 空闲时可借用 GPU/)).toBeInTheDocument()
  expect(screen.queryByText('GPU 已保留给 ComfyUI')).not.toBeInTheDocument()
  expect(screen.queryByText('GPU 状态不可用')).not.toBeInTheDocument()
})
