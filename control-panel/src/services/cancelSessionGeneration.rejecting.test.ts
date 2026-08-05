import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cancelAllSessionGenerations } from './cancelSessionGeneration'
import { useChatStore } from '@/stores/chatStore'

beforeEach(() => {
  localStorage.clear()
  useChatStore.setState({
    sessions: [{
      id: 'session-1',
      title: '测试会话',
      createdAt: 1,
      updatedAt: 1,
      messages: [{
        id: 'assistant-1',
        role: 'assistant',
        content: '',
        generationRequestId: 'request-1',
        awaitingDraft: false,
        ts: 1,
        draft: {
          draftId: 'draft-old',
          previewUrl: '/admin/draft/draft-old/preview',
          mediaType: 'image',
          status: 'rejecting',
          stage: 'rejecting',
          progress: 1,
        },
      }],
    }],
    activeId: 'session-1',
    streaming: false,
    error: null,
    pendingAssistantId: null,
    resumePollingKey: 0,
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('rejecting draft cleanup', () => {
  it('waits for request cancellation and binds the terminal successor draft', async () => {
    const fetchMock = vi.fn().mockResolvedValue(Response.json({
      request_id: 'request-1',
      draft_id: 'draft-new',
      preview_url: '/admin/draft/draft-new/preview',
      status: 'cancelled',
    }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(cancelAllSessionGenerations('session-1')).resolves.toBe(true)

    expect(fetchMock).toHaveBeenCalledWith(
      '/admin/generation/requests/request-1?chat_session_id=session-1',
      expect.objectContaining({ method: 'DELETE', credentials: 'include' }),
    )
    const draft = useChatStore.getState().sessions[0].messages[0].draft
    expect(draft).toMatchObject({
      draftId: 'draft-new',
      previewUrl: '/admin/draft/draft-new/preview',
      status: 'cancelled',
      stage: 'cancelled',
    })
  })
})
