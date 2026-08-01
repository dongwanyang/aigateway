import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import Config from './Config'

const api = vi.hoisted(() => ({
  getComfyUIStatus: vi.fn(async () => ({ data: { available: true, public_url: '', manager_url: '', queue: { running: 0, pending: 0 }, configuration_errors: [] }, message: 'success' })),
  getGenerationPresets: vi.fn(async () => ({ data: [], message: 'success' })),
  getGpuStatus: vi.fn(async () => ({
    data: {
      gateway: { available: true, name: 'GPU', allocated_bytes: 1024, reserved_bytes: 2048, device_used_bytes: 4096, device_free_bytes: 4096, device_total_bytes: 8192 },
      comfyui: { available: true, memory: { total_bytes: 8192, free_bytes: 4096, used_bytes: 4096 } },
      queue: { running: 0, pending: 0 },
      queue_idle: true,
      shared_gpu: true,
      diagnosis: ['gateway_and_comfyui_share_one_gpu'],
    },
    message: 'success',
  })),
  releaseGpuMemory: vi.fn(async () => ({ data: {}, message: 'success' })),
}))
vi.mock('@/api/client', () => api)

afterEach(() => { vi.unstubAllGlobals() })

it('explains resident memory and releases it only while the queue is idle', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/admin/config/schema')) return Response.json({ data: { items: [] }, message: 'success' })
    if (url.endsWith('/admin/config')) return Response.json({ data: { server: { port: 8000 } }, message: 'success', revision: 'r1' })
    throw new Error(`unexpected request: ${url}`)
  }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const user = userEvent.setup()
  render(<QueryClientProvider client={client}><Config /></QueryClientProvider>)
  expect(await screen.findByText(/队列为空只表示没有执行任务/)).toBeInTheDocument()
  expect(screen.getByText(/共用同一块 GPU/)).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: '释放空闲显存' }))
  expect(api.releaseGpuMemory).toHaveBeenCalledTimes(1)
})
