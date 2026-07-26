import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from './client'

const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('browser authentication and streaming contracts', () => {
  it('exchanges and clears an HttpOnly session without persisting the secret', async () => {
    localStorage.setItem('aigateway_api_key', 'legacy-secret')
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({
        data: { key_prefix: 'gw-safe', force_reset: true },
      }))
      .mockResolvedValueOnce(jsonResponse({}))

    const session = await api.saveApiKey('gw-secret-value')
    expect(session).toEqual({ key_prefix: 'gw-safe', force_reset: true })
    expect(localStorage.getItem('aigateway_api_key')).toBeNull()
    expect(api.getSavedApiKey()).toBe('1')
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      expect.stringMatching(/\/auth\/session$/),
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        body: JSON.stringify({ api_key: 'gw-secret-value' }),
      }),
    )

    await api.clearApiKey()
    expect(api.getSavedApiKey()).toBeNull()
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      expect.stringMatching(/\/auth\/session$/),
      { method: 'DELETE', credentials: 'include' },
    )
  })

  it('distinguishes draft JSON from an SSE stream and forwards cancellation', async () => {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('data: ok\n\n'))
        controller.close()
      },
    })
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({
        data: {
          draft_id: 'draft/1',
          preview_url: '/preview',
          generation_params: { media_type: 'video', seed: 7 },
        },
        _meta: { draft_pending_confirmation: true },
      }))
      .mockResolvedValueOnce(new Response(stream, {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      }))
    const controller = new AbortController()

    await expect(api.requestChatCompletion({
      model: 'auto',
      messages: [{ role: 'user', content: 'make video' }],
    } as any, controller.signal)).resolves.toEqual({
      kind: 'draft',
      draftId: 'draft/1',
      previewUrl: '/preview',
      mediaType: 'video',
      generationParams: { media_type: 'video', seed: 7 },
    })
    const streamed = await api.requestChatCompletion({
      model: 'auto',
      messages: [{ role: 'user', content: 'hello' }],
    } as any, controller.signal)
    expect(streamed.kind).toBe('stream')
    if (streamed.kind === 'stream') expect(streamed.body).toBe(stream)
    expect(fetchMock.mock.calls[0][1]).toEqual(expect.objectContaining({
      method: 'POST',
      signal: controller.signal,
      body: expect.stringContaining('"stream":true'),
    }))
  })

  it('returns server authentication errors instead of a false login success', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ error: { message: 'revoked key' } }, 401),
    )
    await expect(api.saveApiKey('revoked')).rejects.toMatchObject({
      message: 'revoked key',
      status: 401,
    })
  })
})

describe('draft and media workflow contracts', () => {
  it('maps every successful draft response into stable UI fields', async () => {
    vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({ status: 'generating' }, 202))
      .mockResolvedValueOnce(jsonResponse({
        preview_data_url: 'data:image/png;base64,abc',
        preview_count: 2,
      }))
      .mockResolvedValueOnce(jsonResponse({
        result_data_url: 'data:image/png;base64,result',
      }))
      .mockResolvedValueOnce(jsonResponse({
        media_type: 'video',
        video_id: 'video-1',
        status: 'queued',
      }))
      .mockResolvedValueOnce(jsonResponse({
        upscaled_url: 'data:image/jpeg;base64,hires',
        target_resolution: [1920, 1080],
        algorithm: 'SUPIR',
      }))
      .mockResolvedValueOnce(jsonResponse({
        new_draft_id: 'draft-2',
        preview_url: '/admin/draft/draft-2/preview',
        attempt_number: 2,
        max_attempts: 5,
      }))
      .mockResolvedValueOnce(jsonResponse({
        status: 'pending',
        expires_at: 123,
        attempt_number: 2,
        max_attempts: 5,
      }))
      .mockResolvedValueOnce(jsonResponse({
        session_id: 'session-1',
        deleted_count: 3,
      }))
      .mockResolvedValueOnce(jsonResponse({
        id: 'video-1',
        status: 'completed',
      }))

    await expect(api.getDraftPreview('draft 1')).resolves.toEqual({ status: 'generating' })
    await expect(api.getDraftPreview('draft 1')).resolves.toEqual({
      previewDataUrl: 'data:image/png;base64,abc',
      previewCount: 2,
    })
    await expect(api.getDraftResult('draft 1')).resolves.toEqual({
      resultDataUrl: 'data:image/png;base64,result',
    })
    await expect(api.confirmDraft('draft 1')).resolves.toEqual({
      videoId: 'video-1',
      status: 'queued',
      mediaType: 'video',
    })
    await expect(api.confirmDraft('draft 1')).resolves.toEqual({
      upscaledUrl: 'data:image/jpeg;base64,hires',
      targetResolution: [1920, 1080],
      algorithm: 'SUPIR',
      mediaType: 'image',
    })
    await expect(api.rejectDraft('draft 1')).resolves.toEqual({
      newDraftId: 'draft-2',
      previewUrl: '/admin/draft/draft-2/preview',
      attemptNumber: 2,
      maxAttempts: 5,
    })
    await expect(api.getDraftStatus('draft 1')).resolves.toEqual({
      status: 'pending',
      expiresAt: 123,
      attemptNumber: 2,
      maxAttempts: 5,
    })
    await expect(api.deleteSessionDrafts('session/1')).resolves.toEqual({
      session_id: 'session-1',
      deleted_count: 3,
    })
    await expect(api.getVideoStatus('video/1')).resolves.toMatchObject({
      id: 'video-1',
      status: 'completed',
    })
  })

  it('rejects malformed successful responses rather than rendering broken media', async () => {
    vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({}))
      .mockResolvedValueOnce(jsonResponse({}))
      .mockResolvedValueOnce(jsonResponse({}))
      .mockResolvedValueOnce(jsonResponse({}))
    await expect(api.getDraftPreview('d')).rejects.toThrow('preview_data_url')
    await expect(api.getDraftResult('d')).rejects.toThrow('result_data_url')
    await expect(api.confirmDraft('d')).rejects.toThrow('upscaled_url')
    await expect(api.rejectDraft('d')).rejects.toThrow('new_draft_id')
  })
})

describe('admin REST client request construction', () => {
  it('sends CRUD bodies, encoded identifiers, filters and debug state', async () => {
    const envelope = { data: {}, message: 'success' }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(
      async () => jsonResponse(envelope),
    )

    await api.createChatCompletion({ model: 'm', messages: [] } as any)
    await api.listModels()
    await api.createEmbeddings({ model: 'e', input: ['x'] } as any)
    await api.listApiKeys(2, 30)
    await api.createApiKey({ user_id: 'alice' } as any)
    await api.deleteApiKey('key/a')
    await api.rotateApiKey('key/a')
    await api.updateApiKeyQuota('key/a', { daily_tokens: 10 })
    await api.getQuota('key/a')
    await api.getL3CacheConfig()
    await api.updateL3CacheConfig({ default_mode: 'manual' })
    await api.listL3Entries({
      page: 2, pageSize: 10, mode: 'manual', userId: 'alice', sortBy: 'hit_count',
    })
    await api.updateL3EntryMode('point/a', 'auto', 12)
    await api.deleteL3Entry('point/a')
    await api.triggerL3Cleanup()
    await api.getRuntimeCapabilities()
    await api.testProviderConnectivity('provider/a')
    await api.fetchProviderModels('provider/a')
    await api.getDebugConfig()
    await api.setPluginDebug('cache/a', true)
    await api.updateDebugSection({ cache: true })
    await api.listGroups()
    await api.createGroup({ name: 'Team' } as any)
    await api.getGroup('grp/a')
    await api.updateGroup('grp/a', { status: 'suspended' } as any)
    await api.deleteGroup('grp/a')
    await api.assignKeyGroup('key/a', 'grp/a', 'private')

    const calls = fetchMock.mock.calls.map(([url, init]) => ({
      url: String(url),
      method: (init as RequestInit | undefined)?.method ?? 'GET',
      body: (init as RequestInit | undefined)?.body,
    }))
    expect(calls).toEqual(expect.arrayContaining([
      expect.objectContaining({
        url: expect.stringContaining('/admin/api-keys?page=2&page_size=30'),
      }),
      expect.objectContaining({
        url: expect.stringContaining('/admin/api-keys/key%2Fa'),
        method: 'DELETE',
      }),
      expect.objectContaining({
        url: expect.stringContaining('/admin/cache/l3/entries/point%2Fa/mode'),
        method: 'PUT',
        body: JSON.stringify({ mode: 'auto', ttl_hours: 12 }),
      }),
      expect.objectContaining({
        url: expect.stringContaining('/admin/plugins/cache%2Fa/debug'),
        method: 'POST',
      }),
      expect.objectContaining({
        url: expect.stringContaining('/admin/api-keys/key%2Fa/group'),
        method: 'PUT',
        body: JSON.stringify({
          group_id: 'grp/a',
          cache_scope: 'private',
        }),
      }),
    ]))
    const debugPut = calls.filter(call =>
      call.url.endsWith('/admin/global-config') && call.method === 'PUT')
    expect(debugPut).toHaveLength(1)
    expect(JSON.parse(String(debugPut[0].body))).toMatchObject({
      debug: { cache: true },
    })
  })
})

describe('logs, configuration, RAG and metrics response contracts', () => {
  it('queries and mutates all persisted admin resources', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    const queue = [
      { data: { items: [], pagination: { page: 1, pageSize: 20, total: 0 } } },
      { data: { deleted: true } },
      { data: { deleted: 2, requested: 2 } },
      { data: { trace_id: 'trace-1' } },
      { data: { server: {} } },
      { data: { updated: true } },
      { data: { documents: [] } },
      { data: { doc_id: 'doc-1' } },
      { data: { deleted: true } },
      { data: { plugins: [] } },
      { data: { name: 'cache', enabled: false } },
      { data: { hot_reload: true, debug_mode: false } },
      { data: { hot_reload: false, debug_mode: true } },
      { data: { prometheus: {}, keys: {}, circuit_breakers: {}, uptime_seconds: 1 } },
      { rows: [{ id: 1 }] },
      { total: { cost_usd: 2 }, by_model: [], by_user: [], by_group: [], by_day: [] },
      { status: 'success', data: { result: [] } },
    ]
    for (const body of queue) fetchMock.mockResolvedValueOnce(jsonResponse(body))

    await api.getRequestLogs({
      page: 1,
      pageSize: 20,
      user_id: 'alice',
      model: 'gpt',
      status: '200',
      cache_only: true,
    })
    await api.deleteAllLogs()
    await api.batchDeleteLogs(['r1', 'r2'])
    await api.getTraceDetail('trace/a')
    await api.getFullConfig()
    await api.updateFullConfig({ server: { port: 9000 } })
    await api.listRagDocuments()
    await api.importRagDocument({ content: 'knowledge', filename: 'doc.txt' })
    await api.deleteRagDocument('doc/a')
    await api.getPluginsConfig()
    await api.togglePlugin('cache', false)
    await api.getGlobalConfig()
    await api.updateGlobalConfig({ hot_reload: false, debug_mode: true })
    await api.getMetricsJson()
    await api.getCostLedger({
      limit: 10,
      offset: 5,
      start: 1,
      end: 2,
      user_id: 'alice',
      group_id: 'team',
      model: 'gpt',
    })
    await api.getCostSummary(7)
    await api.metricsQuery({ query: 'up', start: '1', end: '2', step: '15' })

    const urls = fetchMock.mock.calls.map(([url]) => String(url))
    expect(urls).toEqual(expect.arrayContaining([
      expect.stringContaining(
        '/admin/logs?page=1&page_size=20&user_id=alice&model=gpt&status=200&cache_only=true',
      ),
      expect.stringContaining('/admin/trace/trace%2Fa'),
      expect.stringContaining('/admin/rag/documents/doc%2Fa'),
      expect.stringContaining(
        '/admin/costs/ledger?limit=10&offset=5&start=1&end=2&user_id=alice&group_id=team&model=gpt',
      ),
      expect.stringContaining('/admin/metrics/query_range?query=up&step=15&start=1&end=2'),
    ]))
  })

  it('parses labelled and scalar Prometheus samples and ignores foreign metrics', () => {
    expect(api.parseMetrics([
      '# HELP ignored',
      'gateway_requests_total{model="gpt",status="200"} 3',
      'gateway_queue_depth 2',
      'process_cpu_seconds_total 99',
      '',
    ].join('\n'))).toEqual([
      {
        name: 'gateway_requests_total',
        labels: { model: 'gpt', status: '200' },
        value: 3,
      },
      { name: 'gateway_queue_depth', labels: {}, value: 2 },
    ])
  })
})

describe('Code RAG browser contract', () => {
  it('supports JSON, multipart, task lifecycle and graph traversal', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    const bodies = [
      { task_id: 'task-json', status: 'pending' },
      { task_id: 'task-form', status: 'pending' },
      [{ task_id: 'task-json', status: 'pending' }],
      { task_id: 'task-json', status: 'completed' },
      { task_id: 'task-json', status: 'cancelled' },
      [{ document_id: 'repo-1' }],
      undefined,
      { document_id: 'repo-1', synced_files: 1, refreshed_symbols: 2 },
      [{ name: 'target' }],
      { callers: [{ name: 'caller' }] },
      { callees: [{ name: 'callee' }] },
      { affected: [{ name: 'impact' }] },
      [{ path: 'src/app.py' }],
      [{ name: 'target' }],
    ]
    for (const body of bodies) {
      fetchMock.mockResolvedValueOnce(
        body === undefined ? new Response(null, { status: 204 }) : jsonResponse(body),
      )
    }

    await api.importCodeRepository({
      source_type: 'git',
      git_url: 'https://example.test/repo.git',
      embedding_model: 'embed',
    })
    const form = new FormData()
    form.append('source_type', 'folder')
    form.append('files', new Blob(['code']), 'app.py')
    await api.importCodeRepository(form)
    await api.listCodeImportTasks()
    await api.getCodeImportTask('task/a')
    await api.cancelCodeImportTask('task/a')
    await api.listCodeRepositories()
    await api.deleteCodeRepository('repo/a')
    await api.syncCodeRepository('repo/a')
    await api.queryCodeSymbols('repo/a', 'target', { kind: 'function', limit: 5 })
    await expect(api.getCodeCallers('repo/a', 'target')).resolves.toEqual([{ name: 'caller' }])
    await expect(api.getCodeCallees('repo/a', 'target')).resolves.toEqual([{ name: 'callee' }])
    await expect(api.getCodeImpact('repo/a', 'target', 3)).resolves.toEqual([{ name: 'impact' }])
    await api.listCodeFiles('repo/a')
    await api.listAllSymbols('repo/a', { kind: 'class', limit: 100 })

    const multipartCall = fetchMock.mock.calls[1][1] as RequestInit
    expect(multipartCall.body).toBe(form)
    expect(multipartCall.headers).toEqual({ Authorization: '' })
    const urls = fetchMock.mock.calls.map(([url]) => String(url))
    expect(urls).toEqual(expect.arrayContaining([
      expect.stringContaining('/tasks/task%2Fa/cancel'),
      expect.stringContaining('/repositories/repo%2Fa/query?symbol=target&limit=5&kind=function'),
      expect.stringContaining('/repositories/repo%2Fa/impact?symbol=target&depth=3'),
      expect.stringContaining('/repositories/repo%2Fa/query?symbol=&limit=100&kind=class'),
    ]))
  })
})
