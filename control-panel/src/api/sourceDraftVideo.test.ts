import { afterEach, describe, expect, it, vi } from 'vitest'
import { createVideoDraftFromSource } from './sourceDraftVideo'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('source draft video API', () => {
  it('uses the authenticated admin route and forwards the abort signal', async () => {
    const payload = {
      source_draft_id: 'source-image',
      draft_id: 'video-draft',
      status: 'pending',
      media_type: 'video' as const,
      preview_url: '/admin/draft/video-draft/preview',
      source_image_sha256: 'abc123',
      duration_seconds: 5,
      fps: 8,
      frame_count: 41,
      expires_at: 1234,
    }
    const fetchMock = vi.fn().mockResolvedValue(Response.json(payload))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()

    await expect(createVideoDraftFromSource(
      'source-image',
      {
        motionPrompt: '柯基跑向镜头',
        durationSeconds: 5,
        fps: 8,
        chatSessionId: 'session-1',
      },
      controller.signal,
    )).resolves.toEqual(payload)

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/admin/draft/source-image/video')
    expect(options.method).toBe('POST')
    expect(options.credentials).toBe('include')
    expect(options.signal).toBe(controller.signal)
    expect(JSON.parse(String(options.body))).toEqual({
      motion_prompt: '柯基跑向镜头',
      duration_seconds: 5,
      fps: 8,
      chat_session_id: 'session-1',
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
