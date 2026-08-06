import { afterEach, describe, expect, it, vi } from 'vitest'
import { createVideoDraftFromSource } from './sourceDraftVideo'

// 请求 URL 带上配置的 API base（部署在 /aigateway 子路径下时非空）。
// 断言写死成 '/admin/...' 会在配置了 VITE_API_BASE 的环境里失败，
// 而被测代码的行为其实是正确的。
const API_BASE = import.meta.env.VITE_API_BASE ?? ''

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('source draft video API', () => {
  it('uses the unified chat contract and forwards the stable request identity', async () => {
    const fetchMock = vi.fn().mockResolvedValue(Response.json({
      data: {
        draft_id: 'video-draft',
        status: 'pending',
        preview_url: '/admin/draft/video-draft/preview',
        generation_params: {
          request_id: 'request-1',
          source_draft_id: 'source-image',
          source_image_sha256: 'abc123',
          duration_seconds: 5,
          fps: 8,
          frame_count: 41,
        },
      },
      _meta: { draft_pending_confirmation: true },
    }))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()

    await expect(createVideoDraftFromSource(
      'source-image',
      {
        requestId: 'request-1',
        motionPrompt: '柯基跑向镜头',
        durationSeconds: 5,
        fps: 8,
        chatSessionId: 'session-1',
      },
      controller.signal,
    )).resolves.toMatchObject({
      request_id: 'request-1',
      source_draft_id: 'source-image',
      draft_id: 'video-draft',
      status: 'pending',
      media_type: 'video',
      preview_url: '/admin/draft/video-draft/preview',
      source_image_sha256: 'abc123',
      duration_seconds: 5,
      fps: 8,
      frame_count: 41,
    })

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(`${API_BASE}/admin/draft/source-image/video`)
    expect(options.method).toBe('POST')
    expect(options.credentials).toBe('include')
    expect(options.signal).toBe(controller.signal)
    expect((options.headers as Record<string, string>)['X-Request-ID']).toBe('request-1')
    expect(JSON.parse(String(options.body))).toEqual({
      model: 'auto',
      stream: false,
      chat_session_id: 'session-1',
      messages: [{ role: 'user', content: '柯基跑向镜头' }],
      generation_options: {
        backend: 'local',
        source_draft_id: 'source-image',
        duration_seconds: 5,
        fps: 8,
      },
    })
  })

  it('surfaces the normalized API error message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json(
      {
        error: {
          code: 'source_draft_forbidden',
          message: '无权使用该图片草稿。',
        },
      },
      { status: 403 },
    )))

    await expect(createVideoDraftFromSource(
      'source-image',
      {
        motionPrompt: 'move',
        durationSeconds: 5,
        fps: 8,
        chatSessionId: 'session-1',
      },
    )).rejects.toThrow('无权使用该图片草稿。')
  })
})
