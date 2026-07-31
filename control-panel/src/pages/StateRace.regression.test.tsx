import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import Cache from './Cache'
import Logs from './Logs'

const api = vi.hoisted(() => ({
  getMetricsText: vi.fn(),
  parseMetrics: vi.fn(),
  getL3CacheConfig: vi.fn(),
  updateL3CacheConfig: vi.fn(),
  listL3Entries: vi.fn(),
  updateL3EntryMode: vi.fn(),
  deleteL3Entry: vi.fn(),
  triggerL3Cleanup: vi.fn(),
  getRequestLogs: vi.fn(),
  deleteAllLogs: vi.fn(),
  batchDeleteLogs: vi.fn(),
  getTraceDetail: vi.fn(),
}))
vi.mock('@/api/client', () => api)

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(res => { resolve = res })
  return { promise, resolve }
}

function renderWithClient(element: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}>{element}</QueryClientProvider>)
}

describe('latest-request state guards', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getMetricsText.mockResolvedValue('')
    api.parseMetrics.mockReturnValue([])
    api.getL3CacheConfig.mockResolvedValue({ data: {} })
    api.updateL3CacheConfig.mockResolvedValue({ data: {} })
    api.updateL3EntryMode.mockResolvedValue({})
    api.deleteL3Entry.mockResolvedValue({})
    api.triggerL3Cleanup.mockResolvedValue({ data: { deleted_count: 0 } })
    api.deleteAllLogs.mockResolvedValue({})
    api.batchDeleteLogs.mockResolvedValue({})
  })

  it('loads L3 entries once when the management tab opens', async () => {
    api.listL3Entries.mockResolvedValue({
      data: { items: [], pagination: { total: 0 } },
    })
    const user = userEvent.setup()
    renderWithClient(<Cache />)
    await user.click(screen.getByRole('button', { name: 'L3 缓存管理' }))
    await waitFor(() => expect(api.listL3Entries).toHaveBeenCalledTimes(1))
  })

  it('does not let an older L3 response overwrite a newer filter result', async () => {
    const first = deferred<{ data: { items: Array<{ id: string; promptPreview: string; model: string; userId: string; createdAt: number; expiresAt: number | null; mode: string; hitCount: number; tokenCount: number }>; pagination: { total: number } } }>()
    const second = deferred<{ data: { items: Array<{ id: string; promptPreview: string; model: string; userId: string; createdAt: number; expiresAt: number | null; mode: string; hitCount: number; tokenCount: number }>; pagination: { total: number } } }>()
    api.listL3Entries.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const user = userEvent.setup()
    renderWithClient(<Cache />)
    await user.click(screen.getByRole('button', { name: 'L3 缓存管理' }))
    await waitFor(() => expect(api.listL3Entries).toHaveBeenCalledTimes(1))
    await user.selectOptions(screen.getByRole('combobox'), 'manual')
    await waitFor(() => expect(api.listL3Entries).toHaveBeenCalledTimes(2))
    second.resolve({ data: { items: [{ id: 'new', promptPreview: 'new-result', model: 'm', userId: 'u', createdAt: 1, expiresAt: null, mode: 'manual', hitCount: 0, tokenCount: 1 }], pagination: { total: 1 } } })
    expect(await screen.findByText('new-result')).toBeInTheDocument()
    first.resolve({ data: { items: [{ id: 'old', promptPreview: 'old-result', model: 'm', userId: 'u', createdAt: 1, expiresAt: null, mode: 'auto', hitCount: 0, tokenCount: 1 }], pagination: { total: 1 } } })
    await waitFor(() => expect(screen.queryByText('old-result')).not.toBeInTheDocument())
  })

  it('keeps the newest trace detail when responses complete out of order', async () => {
    api.getRequestLogs.mockResolvedValue({
      data: {
        items: [
          { request_id: 'r-a', trace_id: 'trace-a', user_id: 'u', timestamp: 1, method: 'POST', endpoint: '/v1', model: 'a', status: 200, duration_ms: 1, cache_hit: false, plugin_trace: [] },
          { request_id: 'r-b', trace_id: 'trace-b', user_id: 'u', timestamp: 2, method: 'POST', endpoint: '/v1', model: 'b', status: 200, duration_ms: 1, cache_hit: false, plugin_trace: [] },
        ],
        pagination: { total: 2 },
      },
    })
    const first = deferred<{ data: Record<string, unknown> }>()
    const second = deferred<{ data: Record<string, unknown> }>()
    api.getTraceDetail.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const user = userEvent.setup()
    renderWithClient(<Logs />)
    const traceA = await screen.findByText('trace-a')
    const traceB = await screen.findByText('trace-b')
    await user.click(traceA)
    await user.click(traceB)
    second.resolve({ data: { trace_id: 'trace-b', request_id: 'r-b', user_id: 'u', model: 'b', endpoint: '/v1', status: 200, duration_ms: 1, cache_hit: false, timestamp: 2, events: [], plugin_trace: [], related_requests: [] } })
    expect(await screen.findAllByText('trace-b')).not.toHaveLength(0)
    first.resolve({ data: { trace_id: 'trace-a', request_id: 'r-a', user_id: 'u', model: 'a', endpoint: '/v1', status: 200, duration_ms: 1, cache_hit: false, timestamp: 1, events: [], plugin_trace: [], related_requests: [] } })
    await waitFor(() => expect(screen.queryByText('trace-a', { selector: 'div[style*="word-break"]' })).not.toBeInTheDocument())
  })
})
