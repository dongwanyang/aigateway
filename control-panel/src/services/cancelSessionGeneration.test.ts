import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cancelLatestSessionGeneration } from './cancelSessionGeneration'
import { useChatStore } from '@/stores/chatStore'
import type { ChatSession } from '@/types'

function activeSession(): ChatSession {
  return {
    id: 'session-1',
    title: '测试会话',
    createdAt: 1,
    updatedAt: 1,
    messages: [
      {
        id: 'assistant-1',
        role: 'assistant',
        content: '',
        generationRequestId: 'request-1',
        awaitingDraft: true,
        awaitingDraftSince: 1,
        ts: 1,
        draft: {
          draftId: 'draft-1',
          previewUrl: '/admin/draft/draft-1/preview',
          mediaType: 'video',
          status: 'running',
          stage: 'sampling',
          progress: 0.4,
        },
      },
    ],
  }
}

function message() {
  return useChatStore.getState().sessions[0].messages[0]
}

beforeEach(() => {
  localStorage.clear()
  useChatStore.setState({
    sessions: [activeSession()],
    activeId: 'session-1',
    streaming: false,
    error: null,
    pendingAssistantId: null,
    resumePollingKey: 0,
  })
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('post-response generation cancellation', () => {
  it('shows cancelling until the server confirms the terminal state', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({
        request_id: 'request-1',
        status: 'cancellation_requested',
        retry_after_ms: 100,
      }, { status: 202 }))
      .mockResolvedValueOnce(Response.json({
        request_id: 'request-1',
        draft_id: 'draft-1',
        status: 'cancelled',
      }))
    vi.stubGlobal('fetch', fetchMock)

    const cancellation = cancelLatestSessionGeneration('session-1')

    expect(message().content).toBe('正在停止…')
    expect(message().draft?.status).toBe('running')
    expect(message().draft?.stage).toBe('cancelling')

    await vi.advanceTimersByTimeAsync(100)
    await expect(cancellation).resolves.toBe(true)

    expect(message().content).toBe('已停止')
    expect(message().awaitingDraft).toBe(false)
    expect(message().draft).toMatchObject({
      status: 'cancelled',
      stage: 'cancelled',
      progress: 0,
    })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('does not fabricate cancelled when the server rejects cancellation', async () => {
    const fetchMock = vi.fn().mockResolvedValue(Response.json({
      detail: {
        error: {
          code: 'generation_request_forbidden',
          message: '无权取消该生成请求。',
        },
      },
    }, { status: 403 }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(cancelLatestSessionGeneration('session-1')).resolves.toBe(false)

    expect(message().content).toBe('停止失败，任务仍在运行')
    expect(message().draft?.status).toBe('running')
    expect(message().draft?.stage).toBe('running')
    expect(message().draft?.errorMessage).toContain('停止失败')
    expect(useChatStore.getState().error).toContain('停止生成失败')
  })
})
