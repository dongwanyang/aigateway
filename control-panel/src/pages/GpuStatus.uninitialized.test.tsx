import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import Config from './Config'

const api = vi.hoisted(() => ({
  getComfyUIStatus: vi.fn(async () => ({ data: { available: true, public_url: '', manager_url: '', queue: { running: 0, pending: 0 }, configuration_errors: [] }, message: 'success' })),
  getGenerationPresets: vi.fn(async () => ({ data: [], message: 'success' })),
  getGpuStatus: vi.fn(async () => ({
    data: {
      gateway: { available: false, allocated_bytes: 0, reserved_bytes: 0, device_used_bytes: 0, device_free_bytes: 0, device_total_bytes: 0 },
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

afterEach(() => { vi.unstubAllGlobals() })

it('distinguishes an uninitialized Gateway CUDA context from an unavailable GPU', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/admin/config/schema')) return Response.json({ data: { items: [] }, message: 'success' })
    if (url.endsWith('/admin/config')) return Response.json({ data: { server: { port: 8000 } }, message: 'success', revision: 'r1' })
    throw new Error(`unexpected request: ${url}`)
  }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><Config /></QueryClientProvider>)

  expect(await screen.findByText('未初始化 CUDA')).toBeInTheDocument()
  expect(screen.getByText(/避免空闲占用 ComfyUI 显存/)).toBeInTheDocument()
  expect(screen.getByText(/不表示 GPU 或驱动不可用/)).toBeInTheDocument()
})
