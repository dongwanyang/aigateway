import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  cancelGenerationRequest,
  cancelGenerationRequestAndWait,
  getGenerationRequest,
  newGenerationRequestId,
  waitForGenerationRequestDraft,
  waitForGenerationRequestState,
} from './generationRequest'

afterEach(() => {
  vi.useRealTimers()
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

  it('does not resolve Stop until the persisted request is cancelled', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({
        request_id: 'request-1',
        status: 'cancellation_requested',
        retry_after_ms: 100,
      }, { status: 202 }))
      .mockResolvedValueOnce(Response.json({
        request_id: 'request-1',
        status: 'resolving',
        retry_after_ms: 100,
      }, { status: 202 }))
      .mockResolvedValueOnce(Response.json({
        request_id: 'request-1',
        draft_id: 'draft-1',
        status: 'cancelled',
      }))
    vi.stubGlobal('fetch', fetchMock)

    const cancellation = cancelGenerationRequestAndWait('request-1', 'session-1')
    let settled = false
    void cancellation.finally(() => { settled = true })

    await vi.advanceTimersByTimeAsync(100)
    expect(settled).toBe(false)
    await vi.advanceTimersByTimeAsync(100)

    await expect(cancellation).resolves.toMatchObject({
      draft_id: 'draft-1',
      status: 'cancelled',
    })
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it('treats a non-draft terminal record as a completed transport Stop', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({
        request_id: 'request-text',
        status: 'cancellation_requested',
        retry_after_ms: 100,
      }, { status: 202 }))
      .mockResolvedValueOnce(Response.json({
        request_id: 'request-text',
        status: 'non_draft',
      }))
    vi.stubGlobal('fetch', fetchMock)

    const cancellation = cancelGenerationRequestAndWait('request-text', 'session-1')
    await vi.advanceTimersByTimeAsync(100)

    await expect(cancellation).resolves.toMatchObject({
      status: 'cancelled',
      stage: 'transport_cancelled',
    })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('keeps recovering through transient gateway failures until a draft exists', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({
        error: { code: 'upstream_unavailable', message: 'temporary' },
      }, { status: 503 }))
      .mockResolvedValueOnce(Response.json({
        request_id: 'request-2',
        status: 'resolving',
        retry_after_ms: 100,
      }, { status: 202 }))
      .mockResolvedValueOnce(Response.json({
        request_id: 'request-2',
        draft_id: 'draft-2',
        status: 'running',
        media_type: 'video',
        preview_url: '/admin/draft/draft-2/preview',
      }))
    vi.stubGlobal('fetch', fetchMock)

    const recovery = waitForGenerationRequestDraft('request-2', 'session-2')
    await vi.advanceTimersByTimeAsync(100)
    await vi.advanceTimersByTimeAsync(100)

    await expect(recovery).resolves.toMatchObject({
      draft_id: 'draft-2',
      status: 'running',
    })
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it('returns non-draft from the generic state waiter', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({
        request_id: 'request-text',
        status: 'resolving',
        retry_after_ms: 100,
      }, { status: 202 }))
      .mockResolvedValueOnce(Response.json({
        request_id: 'request-text',
        status: 'non_draft',
      }))
    vi.stubGlobal('fetch', fetchMock)

    const recovery = waitForGenerationRequestState('request-text', 'session-1')
    await vi.advanceTimersByTimeAsync(100)

    await expect(recovery).resolves.toMatchObject({ status: 'non_draft' })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('fails after the registration grace when the POST never reached the server', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn().mockResolvedValue(Response.json({
      request_id: 'request-missing',
      status: 'unregistered',
      retry_after_ms: 5_000,
    }, { status: 202 }))
    vi.stubGlobal('fetch', fetchMock)

    const recovery = waitForGenerationRequestState('request-missing', 'session-1')
    const assertion = expect(recovery).rejects.toMatchObject({
      message: '生成请求未到达服务端，请重新提交',
      code: 'generation_request_not_registered',
      status: 404,
    })

    await vi.advanceTimersByTimeAsync(10_000)
    await assertion
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it('fails draft-only recovery for a non-draft terminal state', async () => {
    const fetchMock = vi.fn().mockResolvedValue(Response.json({
      request_id: 'request-text',
      status: 'non_draft',
    }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      waitForGenerationRequestDraft('request-text', 'session-1'),
    ).rejects.toMatchObject({
      message: '该请求是普通文本响应，断开的响应内容无法恢复',
      code: 'generation_request_not_draft',
      status: 409,
    })
  })

  it('generates request IDs within the server validation contract', () => {
    const requestId = newGenerationRequestId()
    expect(requestId).toMatch(/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/)
  })
})
