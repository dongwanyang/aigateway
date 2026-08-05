import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  cancelGenerationRequest,
  getGenerationRequest,
  newGenerationRequestId,
} from './generationRequest'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('generation request lifecycle client', () => {
  it('queries a response-lost request by stable identity', async () => {
    const fetchMock = vi.fn().mockResolvedValue(Response.json({
      request_id: 'request-1',
      draft_id: 'draft-1',
      status: 'running',
      media_type: 'image',
      preview_url: '/admin/draft/draft-1/preview',
    }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(getGenerationRequest('request-1', 'session-1')).resolves.toMatchObject({
      draft_id: 'draft-1',
      status: 'running',
    })

    expect(fetchMock).toHaveBeenCalledWith(
      '/admin/generation/requests/request-1?chat_session_id=session-1',
      expect.objectContaining({ credentials: 'include' }),
    )
  })

  it('sends server cancellation instead of only aborting fetch', async () => {
    const fetchMock = vi.fn().mockResolvedValue(Response.json({
      request_id: 'request-1',
      draft_id: 'draft-1',
      status: 'cancelled',
    }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(cancelGenerationRequest('request-1', 'session-1')).resolves.toMatchObject({
      status: 'cancelled',
    })

    expect(fetchMock).toHaveBeenCalledWith(
      '/admin/generation/requests/request-1?chat_session_id=session-1',
      expect.objectContaining({ method: 'DELETE', credentials: 'include' }),
    )
  })

  it('generates request IDs within the server validation contract', () => {
    const requestId = newGenerationRequestId()
    expect(requestId).toMatch(/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/)
  })
})
