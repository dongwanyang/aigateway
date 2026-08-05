import { afterEach, describe, expect, it, vi } from 'vitest'
import { confirmDraft, DraftApiError } from './client'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('draft confirmation API errors', () => {
  it('preserves nested machine-readable error codes for chat recovery', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json(
      {
        detail: {
          error: {
            code: 'video_keyframe_integrity_mismatch',
            message: '关键帧已变化，请重新创建视频草稿。',
          },
        },
      },
      { status: 409 },
    )))

    const error = await confirmDraft('video-draft').catch(value => value)

    expect(error).toBeInstanceOf(DraftApiError)
    expect(error).toMatchObject({
      code: 'video_keyframe_integrity_mismatch',
      status: 409,
      userMessage: '关键帧已变化，请重新创建视频草稿。',
    })
    expect((error as Error).message).toContain('video_keyframe_integrity_mismatch')
  })

  it('keeps the successful response contract unchanged', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({
      draft_id: 'video-draft',
      media_type: 'video',
      upscaled_url: 'data:video/mp4;base64,AAAA',
      target_resolution: [512, 288],
      algorithm: 'wan2.2',
    })))

    await expect(confirmDraft('video-draft')).resolves.toEqual({
      upscaledUrl: 'data:video/mp4;base64,AAAA',
      targetResolution: [512, 288],
      algorithm: 'wan2.2',
      mediaType: 'video',
    })
  })
})
