import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

beforeEach(() => {
  vi.resetModules()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('safe full-config revision handling', () => {
  it('binds revisions to config snapshots and advances them after each save', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({
        data: { server: { port: 8000 }, providers: {} },
        message: 'success',
        revision: 'revision-1',
      }))
      .mockResolvedValueOnce(Response.json({
        data: { updated: true },
        message: 'success',
        revision: 'revision-2',
      }))
      .mockResolvedValueOnce(Response.json({
        data: { updated: true },
        message: 'success',
        revision: 'revision-3',
      }))
    vi.stubGlobal('fetch', fetchMock)
    const { getFullConfig, updateFullConfig } = await import('./clientSafe')

    const loaded = await getFullConfig()
    const firstConfig = { ...loaded.data, providers: { openai: {} } }
    await updateFullConfig(firstConfig)

    const secondConfig = { ...firstConfig, embedding: { backend: 'local' } }
    await updateFullConfig(secondConfig)

    const firstPut = fetchMock.mock.calls[1]?.[1] as RequestInit | undefined
    expect(new Headers(firstPut?.headers).get('If-Match')).toBe('"revision-1"')
    expect(JSON.parse(String(firstPut?.body))).toEqual({
      server: { port: 8000 },
      providers: { openai: {} },
    })

    const secondPut = fetchMock.mock.calls[2]?.[1] as RequestInit | undefined
    expect(new Headers(secondPut?.headers).get('If-Match')).toBe('"revision-2"')
  })

  it('uses ETag when the response body omits revision', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json(
        { data: { providers: {} }, message: 'success' },
        { headers: { ETag: 'W/"etag-revision"' } },
      ))
      .mockResolvedValueOnce(Response.json({
        data: { updated: true },
        message: 'success',
      }))
    vi.stubGlobal('fetch', fetchMock)
    const { getFullConfig, updateFullConfig } = await import('./clientSafe')

    const loaded = await getFullConfig()
    await updateFullConfig({ ...loaded.data })

    const put = fetchMock.mock.calls[1]?.[1] as RequestInit | undefined
    expect(new Headers(put?.headers).get('If-Match')).toBe('"etag-revision"')
  })

  it('does not reuse a revision for an unrelated or newly mounted config object', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(Response.json({
      data: { providers: { openai: {} } },
      message: 'success',
      revision: 'revision-1',
    }))
    vi.stubGlobal('fetch', fetchMock)
    const { getFullConfig, updateFullConfig } = await import('./clientSafe')

    await getFullConfig()

    await expect(updateFullConfig({ providers: {} })).rejects.toThrow(
      '配置尚未成功加载，请重新加载后再保存。',
    )
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('rejects a config response that has no usable revision', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(Response.json({
      data: { providers: {} },
      message: 'success',
    }))
    vi.stubGlobal('fetch', fetchMock)
    const { getFullConfig } = await import('./clientSafe')

    await expect(getFullConfig()).rejects.toThrow(
      '配置响应缺少 revision，请重新加载后再保存。',
    )
  })

  it('maps stale revision conflicts to a reload instruction', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({
        data: { providers: {} },
        message: 'success',
        revision: 'revision-1',
      }))
      .mockResolvedValueOnce(Response.json({
        detail: {
          error: {
            code: 'config_version_conflict',
            message: 'configuration changed since it was loaded',
          },
        },
      }, { status: 409 }))
    vi.stubGlobal('fetch', fetchMock)
    const { getFullConfig, updateFullConfig } = await import('./clientSafe')

    const loaded = await getFullConfig()

    await expect(updateFullConfig({ ...loaded.data })).rejects.toThrow(
      '配置已被其他会话修改，请重新加载后再保存。',
    )
  })
})
