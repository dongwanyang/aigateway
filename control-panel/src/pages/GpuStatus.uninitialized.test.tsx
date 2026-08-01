import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Config from './Config'

const state = vi.hoisted(() => ({
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
      comfyui: { available: true, memory: { total_bytes: 16_000, free_bytes: 15_500, used_bytes: 500 } },
      queue: { running: 0, pending: 0 },
      queue_idle: true,
      shared_gpu: true,
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
