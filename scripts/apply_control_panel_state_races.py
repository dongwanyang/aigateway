#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"expected block not found in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# 1) Remove every session-scoped query when identities change.
auth_path = "control-panel/src/contexts/AuthContext.tsx"
replace_once(
    auth_path,
    "import { useQuery, useQueryClient } from '@tanstack/react-query'",
    "import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'",
)
replace_once(
    auth_path,
    '''const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider''',
    '''const AuthContext = createContext<AuthContextValue | null>(null)

export function clearSessionScopedQueries(queryClient: QueryClient) {
  queryClient.removeQueries({
    predicate: query => query.queryKey[0] !== 'auth',
  })
}

export function AuthProvider''',
)
replace_once(
    auth_path,
    '''    const result = await loginWithPassword(username, password)
    const requiresReset = Boolean(result.force_reset)
    setAuthenticated(result.key_prefix, requiresReset)
''',
    '''    const result = await loginWithPassword(username, password)
    const requiresReset = Boolean(result.force_reset)
    clearSessionScopedQueries(queryClient)
    setAuthenticated(result.key_prefix, requiresReset)
''',
)
replace_once(
    auth_path,
    '''    } finally {
      clear()
      queryClient.removeQueries({ queryKey: queryKeys.auth.session })
      queryClient.removeQueries({ queryKey: queryKeys.runtime.capabilities })
    }
''',
    '''    } finally {
      clear()
      clearSessionScopedQueries(queryClient)
      queryClient.setQueryData(queryKeys.auth.session, {
        authenticated: false,
        key_prefix: null,
        scopes: [],
        force_reset: false,
      })
    }
''',
)

# 2) Prevent duplicate and stale L3 entry requests.
cache_path = "control-panel/src/pages/Cache.tsx"
replace_once(
    cache_path,
    "import { useEffect, useState, useCallback } from 'react'",
    "import { useEffect, useState, useCallback, useRef } from 'react'",
)
replace_once(
    cache_path,
    '''  const [showConfigPanel, setShowConfigPanel] = useState(false)
''',
    '''  const [showConfigPanel, setShowConfigPanel] = useState(false)
  const l3RequestSequence = useRef(0)
''',
)
replace_once(
    cache_path,
    '''  useEffect(() => {
    if (activeTab === 'l3-manage') {
      loadL3Config()
      loadL3Entries()
    }
  }, [activeTab])
''',
    '''  useEffect(() => {
    if (activeTab === 'l3-manage') {
      void loadL3Config()
    }
  }, [activeTab])
''',
)
replace_once(
    cache_path,
    '''  const loadL3Entries = useCallback(async () => {
    setL3Loading(true)
    try {
      const resp = await listL3Entries({
        page: l3Page,
        pageSize: 20,
        mode: l3ModeFilter || undefined,
      })
      setL3Entries(resp.data.items)
      setL3Total(resp.data.pagination.total)
    } catch (e) {
      console.error('Failed to load L3 entries:', e)
    } finally {
      setL3Loading(false)
    }
  }, [l3Page, l3ModeFilter])

  useEffect(() => {
    if (activeTab === 'l3-manage') {
      loadL3Entries()
    }
  }, [l3Page, l3ModeFilter, activeTab, loadL3Entries])
''',
    '''  const loadL3Entries = useCallback(async () => {
    const requestId = ++l3RequestSequence.current
    setL3Loading(true)
    try {
      const resp = await listL3Entries({
        page: l3Page,
        pageSize: 20,
        mode: l3ModeFilter || undefined,
      })
      if (requestId !== l3RequestSequence.current) return
      setL3Entries(resp.data.items)
      setL3Total(resp.data.pagination.total)
    } catch (e) {
      if (requestId === l3RequestSequence.current) {
        console.error('Failed to load L3 entries:', e)
      }
    } finally {
      if (requestId === l3RequestSequence.current) setL3Loading(false)
    }
  }, [l3Page, l3ModeFilter])

  useEffect(() => {
    if (activeTab === 'l3-manage') {
      void loadL3Entries()
    }
    return () => {
      l3RequestSequence.current += 1
    }
  }, [activeTab, loadL3Entries])
''',
)

# 3) Ignore stale trace-detail responses.
logs_path = "control-panel/src/pages/Logs.tsx"
replace_once(
    logs_path,
    "import { useEffect, useState, useCallback, useMemo, memo, Fragment } from 'react'",
    "import { useEffect, useState, useCallback, useMemo, memo, Fragment, useRef } from 'react'",
)
replace_once(
    logs_path,
    '''  const [traceLoading, setTraceLoading] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
''',
    '''  const [traceLoading, setTraceLoading] = useState(false)
  const traceRequestSequence = useRef(0)
  const [selected, setSelected] = useState<Set<string>>(new Set())
''',
)
replace_once(
    logs_path,
    '''  const handleTraceClick = useCallback(async (traceId: string) => {
    setTraceLoading(true)
    try {
      const detail = await queryClient.fetchQuery({
        queryKey: queryKeys.logs.trace(traceId),
        queryFn: async () => (await getTraceDetail(traceId)).data,
        staleTime: 30_000,
      })
      setTraceDetail(detail)
    } catch {
      const matched = logs.filter(l => l.trace_id === traceId)
      if (matched.length > 0) {
        const primary = matched[0]
        setTraceDetail({
          trace_id: traceId,
          request_id: primary.request_id,
          user_id: primary.user_id,
          model: primary.model,
          endpoint: primary.endpoint,
          status: primary.status,
          duration_ms: primary.duration_ms,
          cache_hit: primary.cache_hit,
          cache_tier: primary.tier,
          timestamp: primary.timestamp,
          events: [],
          plugin_trace: primary.plugin_trace || [],
          related_requests: matched.slice(1),
        })
      }
    } finally {
      setTraceLoading(false)
    }
  }, [logs, queryClient])
''',
    '''  const handleTraceClick = useCallback(async (traceId: string) => {
    const requestId = ++traceRequestSequence.current
    setTraceLoading(true)
    try {
      const detail = await queryClient.fetchQuery({
        queryKey: queryKeys.logs.trace(traceId),
        queryFn: async () => (await getTraceDetail(traceId)).data,
        staleTime: 30_000,
      })
      if (requestId !== traceRequestSequence.current) return
      setTraceDetail(detail)
    } catch {
      if (requestId !== traceRequestSequence.current) return
      const matched = logs.filter(l => l.trace_id === traceId)
      if (matched.length > 0) {
        const primary = matched[0]
        setTraceDetail({
          trace_id: traceId,
          request_id: primary.request_id,
          user_id: primary.user_id,
          model: primary.model,
          endpoint: primary.endpoint,
          status: primary.status,
          duration_ms: primary.duration_ms,
          cache_hit: primary.cache_hit,
          cache_tier: primary.tier,
          timestamp: primary.timestamp,
          events: [],
          plugin_trace: primary.plugin_trace || [],
          related_requests: matched.slice(1),
        })
      }
    } finally {
      if (requestId === traceRequestSequence.current) setTraceLoading(false)
    }
  }, [logs, queryClient])
''',
)

# 4) Surface overview data failures instead of silently displaying zeros.
overview_path = "control-panel/src/pages/Overview.tsx"
replace_once(
    overview_path,
    '''  const health = healthQuery.data ?? null
  const healthLoading = healthQuery.isLoading
  const stats = metricsQuery.data?.stats ?? statCards
''',
    '''  const health = healthQuery.data ?? null
  const healthLoading = healthQuery.isLoading
  const metricsError = metricsQuery.error instanceof Error
    ? metricsQuery.error.message
    : metricsQuery.isError
      ? '无法加载运营指标'
      : null
  const stats = metricsQuery.data?.stats ?? statCards
''',
)
replace_once(
    overview_path,
    '''      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5">
''',
    '''      {metricsError && (
        <div
          role="alert"
          className="rounded-xl border px-4 py-3 text-sm"
          style={{
            borderColor: 'var(--color-danger)',
            backgroundColor: 'rgba(239, 68, 68, 0.08)',
            color: 'var(--color-danger)',
          }}
        >
          运营指标加载失败：{metricsError}。下方零值仅为占位，不代表当前没有请求。
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5">
''',
)

# 5) Extend the metrics contract without breaking old consumers.
client_path = "control-panel/src/api/client.ts"
replace_once(
    client_path,
    '''export interface MetricsJsonData { prometheus: Record<string, { labels: Record<string, string>; value: number }>; keys:''',
    '''export interface MetricsJsonData { prometheus: Record<string, { labels: Record<string, string>; value: number }>; prometheus_series?: Record<string, Array<{ labels: Record<string, string>; value: number }>>; keys:''',
)

# 6) Strengthen the existing auth integration regression.
auth_test_path = "control-panel/src/contexts/AuthContext.integration.test.tsx"
replace_once(
    auth_test_path,
    '''    const { client } = renderProvider()
    await screen.findByText('anonymous')

    await user.click(screen.getByRole('button', { name: 'login' }))
''',
    '''    const { client } = renderProvider()
    await screen.findByText('anonymous')
    client.setQueryData(['config', 'full'], { secret: 'previous-session' })
    client.setQueryData(['logs', 'list'], { items: ['previous-session'] })

    await user.click(screen.getByRole('button', { name: 'login' }))
''',
)
replace_once(
    auth_test_path,
    '''    expect(api.clearBrowserSession).toHaveBeenCalled()
    expect(client.getQueryData(['auth', 'session'])).toEqual({ authenticated: false })
''',
    '''    expect(api.clearBrowserSession).toHaveBeenCalled()
    expect(client.getQueryData(['config', 'full'])).toBeUndefined()
    expect(client.getQueryData(['logs', 'list'])).toBeUndefined()
    expect(client.getQueryData(['auth', 'session'])).toMatchObject({ authenticated: false })
''',
)

# 7) Add focused race tests.
Path("control-panel/src/pages/StateRace.regression.test.tsx").write_text(
    '''import { render, screen, waitFor } from '@testing-library/react'
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
''',
    encoding="utf-8",
)
