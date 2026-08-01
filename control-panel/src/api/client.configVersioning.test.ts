import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

beforeEach(() => {
  vi.resetModules()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('full-config revision handling', () => {
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
    const { getFullConfig, updateFullConfig } = await import('./client')

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

  it('uses a strong ETag when the response body omits revision', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json(
        { data: { providers: {} }, message: 'success' },
        { headers: { ETag: '"etag-revision"' } },
      ))
      .mockResolvedValueOnce(Response.json(
        { data: { updated: true }, message: 'success' },
        { headers: { ETag: '"etag-revision-2"' } },
      ))
    vi.stubGlobal('fetch', fetchMock)
    const { getFullConfig, updateFullConfig } = await import('./client')

    const loaded = await getFullConfig()
    await updateFullConfig({ ...loaded.data })

    const put = fetchMock.mock.calls[1]?.[1] as RequestInit | undefined
    expect(new Headers(put?.headers).get('If-Match')).toBe('"etag-revision"')
  })

  it('rejects a weak ETag instead of upgrading it to a strong precondition', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(Response.json(
      { data: { providers: {} }, message: 'success' },
      { headers: { ETag: 'W/"weak-revision"' } },
    )))
    const { getFullConfig } = await import('./client')

    await expect(getFullConfig()).rejects.toThrow(
      '配置响应缺少强 revision，请重新加载后再保存。',
    )
  })

  it('does not reuse a revision for an unrelated or newly mounted config object', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(Response.json({
      data: { providers: { openai: {} } },
      message: 'success',
      revision: 'revision-1',
    }))
    vi.stubGlobal('fetch', fetchMock)
    const { getFullConfig, updateFullConfig } = await import('./client')

    await getFullConfig()

    await expect(updateFullConfig({ providers: {} })).rejects.toThrow(
      '配置尚未成功加载，请重新加载后再保存。',
    )
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('rejects a config response that has no usable revision', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(Response.json({
      data: { providers: {} },
      message: 'success',
    })))
    const { getFullConfig } = await import('./client')

    await expect(getFullConfig()).rejects.toThrow(
      '配置响应缺少强 revision，请重新加载后再保存。',
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
    const { getFullConfig, updateFullConfig } = await import('./client')

    const loaded = await getFullConfig()

    await expect(updateFullConfig({ ...loaded.data })).rejects.toThrow(
      '配置已被其他会话修改，请重新加载后再保存。',
    )
  })

  it('keeps update-busy conflicts distinct from stale revisions', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({
        data: { providers: {} },
        message: 'success',
        revision: 'revision-1',
      }))
      .mockResolvedValueOnce(Response.json({
        detail: {
          error: {
            code: 'config_update_busy',
            message: 'another configuration update is in progress',
          },
        },
      }, { status: 409 }))
    vi.stubGlobal('fetch', fetchMock)
    const { getFullConfig, updateFullConfig } = await import('./client')

    const loaded = await getFullConfig()

    await expect(updateFullConfig({ ...loaded.data })).rejects.toThrow(
      '另一个配置更新正在进行，请稍后重试。',
    )
  })
})
