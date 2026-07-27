import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import Overview from './Overview'
import Models from './Models'
import Plugins from './Plugins'
import Costs from './Costs'
import Quotas from './Quotas'
import Cache from './Cache'
import Logs from './Logs'
import Knowledge from './Knowledge'
import KnowledgeCodeTab from './KnowledgeCodeTab'
import Config from './Config'
import Chat from './Chat'
import { useChatStore } from '@/stores/chatStore'

vi.mock('recharts', () => {
  const Box = ({ children }: { children?: React.ReactNode }) => <div>{children}</div>
  return {
    ResponsiveContainer: Box,
    AreaChart: Box,
    Area: Box,
    LineChart: Box,
    Line: Box,
    BarChart: Box,
    Bar: Box,
    PieChart: Box,
    Pie: Box,
    Cell: Box,
    XAxis: Box,
    YAxis: Box,
    CartesianGrid: Box,
    Tooltip: Box,
    Legend: Box,
  }
})

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    isAuthenticated: true,
    isLoading: false,
    keyPrefix: 'gw-test',
    login: vi.fn(),
    logout: vi.fn(),
  }),
  AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}))

const apiKey = {
  id: 'key-1',
  key_prefix: 'gw-test',
  user_id: 'alice',
  group_id: 'grp-team',
  group_name: 'Team',
  cache_scope: 'group',
  created_at: '2026-01-01T00:00:00Z',
  last_used_at: '2026-01-02T00:00:00Z',
  status: 'active',
  quotas: {
    daily_tokens_used: 100,
    daily_tokens_limit: 1000,
    monthly_cost_used: 1,
    monthly_cost_limit: 10,
    rpm_current: 2,
    rpm_limit: 60,
    tpm_current: 100,
    tpm_limit: 1000,
  },
  usage_percentage: { daily_tokens: 0.1, monthly_cost: 0.1 },
}

const group = {
  group_id: 'grp-team',
  name: 'Team',
  status: 'active',
  member_count: 1,
  daily_tokens_limit: 1000,
  daily_tokens_used: 100,
  monthly_cost_limit: 10,
  monthly_cost_used: 1,
  rate_limit_rpm: 60,
  rate_limit_tpm: 1000,
  created_at: '2026-01-01T00:00:00Z',
}

const fullConfig = {
  server: { host: '0.0.0.0', port: 8000 },
  providers: {
    openai: {
      api_key: 'sk-masked***',
      base_url: 'https://api.openai.com/v1',
      model_grouper: [{
        models: [
          { name: 'gpt-4o', modality: ['llm'] },
          'gpt-4o-mini',
          { name: '', modality: [] },
        ],
        fallback_models: ['gpt-4o-mini'],
        pricing: { 'gpt-4o': { prompt: 0.01, completion: 0.03 } },
      }],
      num_retries: 3,
      retry_after: 1000,
      timeout: 120,
    },
  },
  embedding: {
    backend: 'sentence_transformers',
    model: 'Qwen/embed',
    vector_dim: 1024,
    openai_model: 'text-embedding-3-small',
  },
}

function responseFor(input: RequestInfo | URL, init?: RequestInit): Response {
  const url = String(input)
  const method = init?.method ?? 'GET'
  if (url.endsWith('/admin/console/chat/completions')) {
    return new Response([
      'data: {"choices":[{"delta":{"content":"Hello "}}],"_meta":{"routed_to":{"intent":"understanding","model":"gpt-4o"}}}',
      '',
      'data: {"choices":[{"delta":{"content":"world"}}]}',
      '',
      'data: [DONE]',
      '',
    ].join('\n'), { headers: { 'Content-Type': 'text/event-stream' } })
  }
  if (url.endsWith('/health')) {
    return Response.json({
      data: {
        status: 'healthy',
        uptime_seconds: 3661,
        dependencies: {
          redis: { status: 'connected', latency_ms: 1 },
          qdrant: { status: 'connected', latency_ms: 2 },
        },
      },
      message: 'success',
    })
  }
  if (url.endsWith('/metrics')) {
    return new Response([
      'gateway_http_requests_total{model="gpt"} 10',
      'gateway_cost_by_model_total{model="gpt"} 1.5',
      'gateway_cost_by_user_total{user_id="alice"} 1.5',
      'gateway_cache_hits_total 8',
      'gateway_cache_misses_total 2',
      'gateway_tokens_saved_total 500',
      'gateway_request_duration_seconds_count{model="gpt"} 10',
      'gateway_request_duration_seconds_sum{model="gpt"} 2',
      'gateway_request_duration_seconds_bucket{model="gpt",le="0.1"} 5',
      'gateway_request_duration_seconds_bucket{model="gpt",le="0.5"} 10',
    ].join('\n'))
  }
  if (url.includes('/admin/debug/config')) {
    return Response.json({
      data: {
        frontend: true,
        entry: true,
        cache: false,
        bridge: false,
        plugins_enabled: true,
        per_plugin: { prompt_cache: true },
      },
      message: 'success',
    })
  }
  if (url.includes('/admin/debug/plugins/')) {
    const plugin = decodeURIComponent(url.split('/admin/debug/plugins/')[1]?.split('?')[0] ?? '')
    return Response.json({ data: { name: plugin, enabled: false }, message: 'success' })
  }
  if (url.includes('/admin/config')) {
    if (method === 'PUT') return Response.json({ data: { updated: true }, message: 'success' })
    return Response.json({ data: fullConfig, message: 'success' })
  }
  if (url.includes('/admin/plugins-config')) {
    if (method === 'PUT') return Response.json({ data: { name: 'pii_detector', enabled: false }, message: 'success' })
    return Response.json({
      data: {
        plugins: [
          {
            name: 'pii_detector',
            enabled: true,
            depends_on: [],
            config: {},
            pipeline_kind: 'understanding',
            priority: 1,
            debug: false,
          },
          {
            name: 'prompt_cache',
            enabled: true,
            depends_on: [],
            config: {},
            pipeline_kind: 'understanding',
            priority: 2,
            debug: true,
          },
          {
            name: 'prompt_compress',
            enabled: false,
            depends_on: [],
            config: {},
            pipeline_kind: 'generation',
            priority: 3,
            debug: null,
          },
        ],
      },
      message: 'success',
    })
  }
  if (url.includes('/admin/global-config')) {
    return Response.json({
      data: {
        hot_reload: true,
        debug_mode: false,
        debug: {},
      },
      message: 'success',
    })
  }
  if (url.includes('/admin/providers/') && url.endsWith('/test')) {
    return Response.json({ data: { success: true, latency_ms: 12, error: null }, message: 'success' })
  }
  if (url.includes('/admin/providers/') && url.endsWith('/models')) {
    return Response.json({ data: { models: ['gpt-4o', 'gpt-4.1'], source: 'remote' }, message: 'success' })
  }
  if (url.includes('/admin/costs/summary')) {
    return Response.json({
      total: {
        requests: 10,
        tokens_in: 100,
        tokens_out: 50,
        tokens_total: 150,
        cost_usd: 1.5,
        cache_hits: 4,
      },
      by_model: [{ k: 'gpt-4o', requests: 10, tokens_total: 150, cost_usd: 1.5, cache_hits: 4 }],
      by_user: [{ k: 'alice', requests: 10, tokens_total: 150, cost_usd: 1.5, cache_hits: 4 }],
      by_group: [{ k: 'team', requests: 10, tokens_total: 150, cost_usd: 1.5, cache_hits: 4 }],
      by_day: [{ k: '2026-07-26', requests: 10, tokens_total: 150, cost_usd: 1.5 }],
    })
  }
  if (url.includes('/admin/metrics/query_range')) {
    return Response.json({
      status: 'success',
      data: {
        resultType: 'matrix',
        result: [{
          metric: { model: 'gpt-4o' },
          values: [{ timestamp: '1', value: '0.1' }, { timestamp: '2', value: '0.2' }],
        }],
      },
    })
  }
  if (url.includes('/admin/api-keys')) {
    if (method !== 'GET') return Response.json({ data: apiKey, message: 'success' })
    return Response.json({
      data: { items: [apiKey], pagination: { page: 1, pageSize: 20, total: 1 } },
      message: 'success',
    })
  }
  if (url.includes('/admin/groups')) {
    return Response.json({
      data: { items: [group], total: 1, ...group },
      message: 'success',
    })
  }
  if (url.includes('/admin/cache/l3/config')) {
    return Response.json({
      data: {
        default_mode: 'auto',
        auto_cleanup_interval_minutes: 60,
        default_ttl_hours: 24,
        min_ttl_hours: 1,
        max_ttl_hours: 720,
      },
      message: 'success',
    })
  }
  if (url.includes('/admin/cache/l3/entries')) {
    return Response.json({
      data: {
        items: [{
          id: 'point-1',
          promptPreview: 'hello',
          model: 'gpt-4o',
          userId: 'alice',
          createdAt: 1,
          expiresAt: 2,
          mode: 'auto',
          hitCount: 3,
          tokenCount: 10,
        }],
        pagination: { page: 1, pageSize: 20, total: 1 },
      },
      message: 'success',
    })
  }
  if (url.includes('/admin/cache/l3/cleanup')) {
    return Response.json({ data: { deleted_count: 1 }, message: 'success' })
  }
  if (url.includes('/admin/logs')) {
    if (method !== 'GET') return Response.json({ data: { deleted: true }, message: 'success' })
    return Response.json({
      data: {
        items: [{
          request_id: 'req-1',
          trace_id: 'trace-1',
          user_id: 'alice',
          timestamp: 1_700_000_000,
          method: 'POST',
          endpoint: '/v1/chat/completions',
          model: 'gpt-4o',
          status: 200,
          duration_ms: 25,
          cache_hit: true,
          tier: 'L1',
          plugin_trace: [],
        }],
        pagination: { page: 1, pageSize: 50, total: 1 },
      },
      message: 'success',
    })
  }
  if (url.includes('/admin/trace/')) {
    return Response.json({
      data: {
        trace_id: 'trace-1',
        request_id: 'req-1',
        user_id: 'alice',
        model: 'gpt-4o',
        endpoint: '/v1/chat/completions',
        status: 200,
        duration_ms: 25,
        cache_hit: true,
        cache_tier: 'L1',
        timestamp: 1,
        events: [],
        plugin_trace: [],
        related_requests: [],
      },
      message: 'success',
    })
  }
  if (url.includes('/admin/rag/documents')) {
    if (method === 'POST') {
      return Response.json({
        data: { doc_id: 'doc-new', chunk_count: 3, total_tokens: 42, elapsed_ms: 8 },
        message: 'success',
      })
    }
    return Response.json({
      data: {
        documents: [{
          doc_id: 'doc-1',
          filename: 'guide.txt',
          file_type: 'text',
          chunk_count: 2,
          chunk_strategy: 'paragraph',
          chunk_size: 512,
          chunk_overlap: 64,
          total_tokens: 20,
          created_at: 1,
          url: '',
        }],
        deleted: true,
      },
      message: 'success',
    })
  }
  if (url.includes('/admin/rag/code/tasks')) {
    return Response.json([])
  }
  if (url.endsWith('/admin/rag/code/import')) {
    return Response.json({ task_id: 'task-new', status: 'pending' })
  }
  if (url.endsWith('/admin/rag/code/repositories/repo-1/sync')) {
    return Response.json({ document_id: 'repo-1', synced_files: 2, refreshed_symbols: 4, deleted_files: 1 })
  }
  if (url.includes('/admin/rag/code/repositories')) {
    if (method === 'DELETE') return new Response(null, { status: 204 })
    return Response.json([{
      document_id: 'repo-1',
      source_type: 'git',
      source_label: 'https://example.test/repo.git',
      file_count: 1,
      language_summary: ['python'],
      function_count: 2,
      class_count: 1,
      chunk_count: 3,
      embedding_model: 'embed',
      import_time: '2026-01-01',
    }])
  }
  if (url.includes('/admin/capabilities')) {
    const available = {
      installed: true,
      configured: true,
      available: true,
      install_command: null,
      reason: null,
    }
    return Response.json({
      data: {
        profile: 'all',
        capabilities: {
          core: available,
          rag: available,
          code_rag: available,
          vision: available,
          upscaling: available,
          gpu: { ...available, available: false, reason: 'CPU runtime' },
        },
      },
      message: 'success',
    })
  }
  return Response.json({ data: {}, message: 'success' })
}

function renderPage(component: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        {component}
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  useChatStore.setState({
    sessions: [],
    activeId: null,
    streaming: false,
    error: null,
    pendingAssistantId: null,
    resumePollingKey: 0,
  })
  localStorage.clear()
  vi.stubGlobal('fetch', vi.fn(responseFor))
  vi.stubGlobal('confirm', vi.fn(() => true))
  vi.stubGlobal('alert', vi.fn())
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
  })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('control panel pages against production API response shapes', () => {
  it.each([
    ['概览', <Overview />, true],
    ['模型配置', <Models />, true],
    ['插件管理', <Plugins />, true],
    ['成本分析', <Costs />, true],
    ['配额管理', <Quotas />, true],
    ['缓存监控', <Cache />, true],
    ['请求日志', <Logs />, true],
    ['知识库', <Knowledge />, true],
    ['系统配置', <Config />, true],
    ['新对话', <Chat />, false],
  ])('renders %s with its real data-loading behavior', async (heading, page, expectsFetch) => {
    renderPage(page)
    expect(screen.getByRole('heading', { name: heading })).toBeInTheDocument()
    if (expectsFetch) await waitFor(() => expect(fetch).toHaveBeenCalled())
  })

  it('loads and edits provider configuration through visible controls', async () => {
    const user = userEvent.setup()
    renderPage(<Models />)
    expect(await screen.findByText('openai')).toBeInTheDocument()
    expect(await screen.findByText('gpt-4o')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /保存配置/ }))
    await waitFor(() => expect(screen.getByText(/模型配置已保存并生效/)).toBeInTheDocument())
    expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/admin\/config$/),
      expect.objectContaining({ method: 'PUT' }),
    )
  })

  it('tests provider connectivity, fetches remote models and completes quick-add', async () => {
    const user = userEvent.setup()
    renderPage(<Models />)
    expect(await screen.findByText('openai')).toBeInTheDocument()
    await user.click(screen.getByTitle('测试连通性'))
    expect(await screen.findByText('✓ 12ms')).toBeInTheDocument()
    await user.click(screen.getByTitle('获取模型列表'))
    expect(await screen.findByText(/远程可用模型 \(2\)/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /快速添加/ }))
    expect(screen.getByText('选择模型提供商')).toBeInTheDocument()
    await user.click(screen.getByText('Anthropic (Claude)'))
    const key = screen.getByPlaceholderText(/sk-ant/)
    await user.type(key, 'sk-ant-real-test-key')
    await user.click(screen.getByRole('button', { name: /确认添加/ }))
    expect(await screen.findByText(/已添加提供商 "anthropic"/)).toBeInTheDocument()
  })

  it('edits a model through the modality and pricing dialog', async () => {
    const user = userEvent.setup()
    renderPage(<Models />)
    await screen.findByText('gpt-4o')
    await user.click(screen.getAllByTitle('编辑模型')[0])
    expect(screen.getByRole('heading', { name: /编辑模型.*openai/ })).toBeInTheDocument()
    const name = screen.getByPlaceholderText('如: gpt-4o')
    await user.clear(name)
    await user.type(name, 'gpt-4.1')
    await user.click(screen.getByText('多模态理解 (mllm)'))
    await user.clear(screen.getByPlaceholderText('0.000005'))
    await user.type(screen.getByPlaceholderText('0.000005'), '0.02')
    await user.click(screen.getByRole('button', { name: '保存' }))
    expect(await screen.findByText('gpt-4.1')).toBeInTheDocument()
    expect(screen.getByText('mllm')).toBeInTheDocument()
  })

  it('switches quota tabs, searches keys and opens creation forms', async () => {
    const user = userEvent.setup()
    renderPage(<Quotas />)
    expect(await screen.findByText(/gw-test/)).toBeInTheDocument()
    await user.type(screen.getByPlaceholderText(/搜索用户/), 'alice')
    expect(screen.getByText('alice')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '用户组' }))
    expect((await screen.findAllByText('Team')).length).toBeGreaterThan(0)
    await user.click(screen.getByRole('button', { name: /创建用户组/ }))
    expect(screen.getByRole('heading', { name: '创建新用户组' })).toBeInTheDocument()
  })

  it('creates an API key and persists edited quota values', async () => {
    const user = userEvent.setup()
    renderPage(<Quotas />)
    await screen.findByText(/gw-test/)
    await user.click(screen.getByRole('button', { name: /创建 API Key/ }))
    await user.type(screen.getByPlaceholderText('user-id'), 'bob')
    await user.click(screen.getByRole('button', { name: '创建' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/admin\/api-keys$/),
      expect.objectContaining({ method: 'POST' }),
    ))

    await user.click(screen.getByTitle('修改配额'))
    expect(screen.getByRole('heading', { name: /修改配额.*alice/ })).toBeInTheDocument()
    const numberInputs = screen.getAllByRole('spinbutton')
    await user.clear(numberInputs[0])
    await user.type(numberInputs[0], '2000')
    await user.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/admin\/api-keys\/key-1$/),
      expect.objectContaining({ method: 'PUT', body: expect.stringContaining('2000') }),
    ))
  })

  it('creates, edits and deletes quota groups through the management forms', async () => {
    const user = userEvent.setup()
    renderPage(<Quotas />)
    await screen.findByText(/gw-test/)
    await user.click(screen.getByRole('button', { name: '用户组' }))
    await screen.findAllByText('Team')

    await user.click(screen.getByRole('button', { name: /创建用户组/ }))
    await user.type(screen.getByPlaceholderText('e.g. engineering'), 'platform')
    await user.click(screen.getByRole('button', { name: '创建' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/admin\/groups$/),
      expect.objectContaining({ method: 'POST', body: expect.stringContaining('"name":"platform"') }),
    ))

    await user.click(screen.getByTitle('编辑'))
    expect(screen.getByRole('heading', { name: /编辑用户组.*Team/ })).toBeInTheDocument()
    const groupLimits = screen.getAllByRole('spinbutton')
    await user.clear(groupLimits[0])
    await user.type(groupLimits[0], '5000')
    await user.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/admin\/groups\/grp-team$/),
      expect.objectContaining({ method: 'PUT', body: expect.stringContaining('"daily_tokens":5000') }),
    ))

    await user.click(screen.getByTitle('删除'))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/admin\/groups\/grp-team$/),
      expect.objectContaining({ method: 'DELETE' }),
    ))
  })

  it('filters cache entries and executes an explicit cleanup action', async () => {
    const user = userEvent.setup()
    renderPage(<Cache />)
    await user.click(screen.getByRole('button', { name: 'L3 缓存管理' }))
    expect(await screen.findByText('hello')).toBeInTheDocument()
    const cleanup = screen.getByRole('button', { name: /清理过期/ })
    await user.click(cleanup)
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/admin\/cache\/l3\/cleanup$/),
      expect.objectContaining({ method: 'POST' }),
    ))
  })

  it('opens a trace from a persisted log row', async () => {
    const user = userEvent.setup()
    renderPage(<Logs />)
    expect(await screen.findByText('req-1')).toBeInTheDocument()
    await user.click(screen.getByTitle('点击查看全链路追踪'))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/admin\/trace\/trace-1$/),
      expect.anything(),
    ))
  })

  it('expands a log row and performs confirmed bulk deletion', async () => {
    const user = userEvent.setup()
    renderPage(<Logs />)
    const request = await screen.findByText('req-1')
    await user.click(request.closest('tr')!)
    expect(screen.getByText('状态码')).toBeInTheDocument()
    const checkbox = screen.getByRole('checkbox', { name: '选择 req-1' })
    await user.click(checkbox)
    await user.click(screen.getByRole('button', { name: /删除选中/ }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/admin\/logs\/batch-delete$/),
      expect.objectContaining({ method: 'POST', body: '{"request_ids":["req-1"]}' }),
    ))
  })

  it('opens the Code RAG workspace from the knowledge page', async () => {
    const user = userEvent.setup()
    renderPage(<Knowledge />)
    expect(await screen.findByText('guide.txt')).toBeInTheDocument()
    const codeTab = screen.getByRole('button', { name: /代码知识库|Code RAG/ })
    await user.click(codeTab)
    await waitFor(() => expect(screen.getByText(/repo-1|example.test/)).toBeInTheDocument())
  })

  it('imports a URL document with explicit chunking and deletes an existing document', async () => {
    const user = userEvent.setup()
    renderPage(<Knowledge />)
    expect(await screen.findByText('guide.txt')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /导入文档/ }))
    await user.type(screen.getByPlaceholderText('https://example.com/article...'), 'https://docs.example.test/guide')
    const sizes = screen.getAllByRole('spinbutton')
    await user.clear(sizes[0])
    await user.type(sizes[0], '256')
    await user.click(screen.getByRole('button', { name: '开始导入' }))
    expect(await screen.findByText(/导入成功: 3 个分块, 42 tokens/)).toBeInTheDocument()
    expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/admin\/rag\/documents$/),
      expect.objectContaining({
        method: 'POST',
        body: expect.stringContaining('"chunk_size":256'),
      }),
    )
    await user.click(screen.getByTitle('删除文档'))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/admin\/rag\/documents\/doc-1$/),
      expect.objectContaining({ method: 'DELETE' }),
    ))
  })

  it('persists plugin, debug and hot-reload toggle changes', async () => {
    const user = userEvent.setup()
    renderPage(<Plugins />)
    await screen.findByText('pii_detector')
    const toggles = screen.getAllByRole('checkbox')
    await user.click(toggles[0])
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/admin\/plugins-config$/),
      expect.objectContaining({ method: 'PUT', body: expect.stringContaining('"enabled":false') }),
    ))
    await user.click(screen.getAllByTitle('Debug 日志')[0])
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/admin\/debug\/plugins\/prompt_cache$/),
      expect.objectContaining({ method: 'PUT', body: expect.stringContaining('"enabled":false') }),
    ))
    const hotReload = screen.getByText('热重载').parentElement?.parentElement?.querySelector('input') as HTMLInputElement
    await user.click(hotReload)
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/admin\/global-config$/),
      expect.objectContaining({ method: 'PUT', body: '{"hot_reload":false}' }),
    ))
  })

  it('validates, submits, syncs and deletes Code RAG repositories', async () => {
    const user = userEvent.setup()
    renderPage(<KnowledgeCodeTab />)
    expect(await screen.findByText('https://example.test/repo.git')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /Git 仓库 URL/ }))
    await user.type(screen.getByPlaceholderText('https://github.com/org/repo'), 'git://unsafe/repo')
    await user.click(screen.getByRole('button', { name: /开始导入/ }))
    expect(await screen.findByText(/git_url 必须以 https:\/\//)).toBeInTheDocument()
    const gitUrl = screen.getByPlaceholderText('https://github.com/org/repo')
    await user.clear(gitUrl)
    await user.type(gitUrl, 'https://github.com/acme/repo')
    await user.type(screen.getByPlaceholderText('main'), 'develop')
    await user.click(screen.getByRole('button', { name: /开始导入/ }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/admin\/rag\/code\/import$/),
      expect.objectContaining({ method: 'POST', body: expect.stringContaining('develop') }),
    ))
    expect(await screen.findByText('排队中')).toBeInTheDocument()

    await user.click(screen.getByTitle('增量同步'))
    expect(await screen.findByText(/同步完成: 2 文件、4 符号刷新、1 文件移除/)).toBeInTheDocument()
    await user.click(screen.getByTitle('删除'))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/admin\/rag\/code\/repositories\/repo-1$/),
      expect.objectContaining({ method: 'DELETE' }),
    ))
    expect(screen.queryByText('https://example.test/repo.git')).not.toBeInTheDocument()
  })

  it('validates and saves edited JSON configuration', async () => {
    const user = userEvent.setup()
    renderPage(<Config />)
    const editor = await screen.findByRole('textbox')
    fireEvent.change(editor, { target: { value: JSON.stringify({ server: { port: 9000 } }, null, 2) } })
    await user.click(screen.getByRole('button', { name: /保存配置/ }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/admin\/config$/),
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({ server: { port: 9000 } }),
      }),
    ))
  })

  it('sends a real SSE chat request, renders routed output and clears the conversation', async () => {
    const user = userEvent.setup()
    renderPage(<Chat />)
    const composer = await screen.findByPlaceholderText(/输入消息/)
    await user.type(composer, 'Explain routing')
    await user.click(screen.getByRole('button', { name: '发送' }))

    expect(await screen.findByText('Hello world')).toBeInTheDocument()
    expect(screen.getAllByText('Explain routing').length).toBeGreaterThan(0)
    expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/admin\/console\/chat\/completions$/),
      expect.objectContaining({
        method: 'POST',
        body: expect.stringContaining('"content":"Explain routing"'),
      }),
    )
    await user.click(screen.getByRole('button', { name: /清空/ }))
    expect(screen.queryByText('Hello world')).not.toBeInTheDocument()
    const chatState = useChatStore.getState()
    expect(chatState.sessions.find(session => session.id === chatState.activeId)?.messages).toEqual([])
  })
})
