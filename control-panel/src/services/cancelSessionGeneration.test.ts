import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  cancelAllSessionGenerations,
  cancelLatestSessionGeneration,
} from './cancelSessionGeneration'
import { useChatStore } from '@/stores/chatStore'
import type { ChatPageMessage, ChatSession } from '@/types'

function activeMessage(
  id = 'assistant-1',
  requestId = 'request-1',
  draftId = 'draft-1',
): ChatPageMessage {
  return {
    id,
    role: 'assistant',
    content: '',
    generationRequestId: requestId,
    awaitingDraft: true,
    awaitingDraftSince: 1,
    ts: 1,
    draft: {
      draftId,
      previewUrl: `/admin/draft/${draftId}/preview`,
      mediaType: 'video',
      status: 'running',
      stage: 'sampling',
      progress: 0.4,
    },
  }
}

function activeSession(messages: ChatPageMessage[] = [activeMessage()]): ChatSession {
  return {
    id: 'session-1',
    title: '测试会话',
    createdAt: 1,
    updatedAt: 1,
    messages,
  }
}

function message(index = 0) {
  return useChatStore.getState().sessions[0].messages[index]
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

  it('keeps unconfirmed ComfyUI cancellation as a running warning', async () => {
    const fetchMock = vi.fn().mockResolvedValue(Response.json({
      detail: {
        error: {
          code: 'comfyui_cancellation_unconfirmed',
          message: 'ComfyUI 未确认任务已停止，任务状态已恢复并将继续跟踪。',
        },
      },
    }, { status: 503 }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(cancelLatestSessionGeneration('session-1')).resolves.toBe(false)

    expect(message().content).toBe('停止未确认，任务继续运行')
    expect(message().error).toBe(false)
    expect(message().awaitingDraft).toBe(true)
    expect(message().draft).toMatchObject({
      status: 'running',
      stage: 'cancellation_unconfirmed',
      errorMessage: '停止未确认，任务继续运行',
    })
    expect(useChatStore.getState().error).toContain('任务将继续运行')
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

  it('confirms every active request before destructive session cleanup', async () => {
    useChatStore.setState({
      sessions: [activeSession([
        activeMessage('assistant-1', 'request-1', 'draft-1'),
        activeMessage('assistant-2', 'request-2', 'draft-2'),
      ])],
    })
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({
        request_id: 'request-1',
        draft_id: 'draft-1',
        status: 'cancelled',
      }))
      .mockResolvedValueOnce(Response.json({
        request_id: 'request-2',
        draft_id: 'draft-2',
        status: 'cancelled',
      }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(cancelAllSessionGenerations('session-1')).resolves.toBe(true)

    expect(message(0).draft?.status).toBe('cancelled')
    expect(message(1).draft?.status).toBe('cancelled')
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      '/admin/generation/requests/request-1?chat_session_id=session-1',
      expect.objectContaining({ method: 'DELETE' }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      '/admin/generation/requests/request-2?chat_session_id=session-1',
      expect.objectContaining({ method: 'DELETE' }),
    )
  })
})
