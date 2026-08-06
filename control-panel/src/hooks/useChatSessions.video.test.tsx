import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useChatStore } from '@/stores/chatStore'
import type { ChatPageMessage } from '@/types'
import { useChatSessions } from './useChatSessions'

const consoleChat = vi.hoisted(() => ({ requestChatCompletion: vi.fn() }))
const client = vi.hoisted(() => ({
  cancelDraft: vi.fn(),
  getDraftResult: vi.fn(),
  confirmDraft: vi.fn(),
  rejectDraft: vi.fn(),
  deleteSessionDrafts: vi.fn(),
  getVideoStatus: vi.fn(),
}))

vi.mock('@/api/consoleChat', () => consoleChat)
vi.mock('@/api/client', () => client)

function seedVideoMessage(overrides: Partial<ChatPageMessage> = {}) {
  const message: ChatPageMessage = {
    id: 'assistant-1',
    role: 'assistant',
    content: '',
    intent: 'generation:video',
    videoId: 'vid-1',
    ts: 1,
    ...overrides,
  }
  useChatStore.setState({
    sessions: [{
      id: 'session-1',
      title: '视频会话',
      messages: [
        { id: 'user-1', role: 'user', content: '让它动起来', ts: 1 },
        message,
      ],
      createdAt: 1,
      updatedAt: 1,
    }],
    activeId: 'session-1',
    streaming: false,
    error: null,
    pendingAssistantId: null,
    resumePollingKey: 0,
  })
}

function activeMessage(id = 'assistant-1'): ChatPageMessage | undefined {
  const state = useChatStore.getState()
  return state.sessions
    .find(session => session.id === state.activeId)
    ?.messages.find(message => message.id === id)
}

beforeEach(() => {
  vi.clearAllMocks()
  client.cancelDraft.mockResolvedValue({ draft_id: 'd1', cancelled: true })
  localStorage.clear()
})

describe('video result resolution in chat sessions', () => {
  it('resolves a completed video whose URL only exists in metadata.url', async () => {
    client.getVideoStatus.mockResolvedValue({
      status: 'completed',
      metadata: { url: 'https://cdn.test/final.mp4' },
    })
    seedVideoMessage()

    renderHook(() => useChatSessions())

    await waitFor(() => {
      expect(activeMessage()?.videoUrl).toBe('https://cdn.test/final.mp4')
    })
    expect(activeMessage()?.videoPhase).toBe('succeeded')
  })

  it('surfaces an upstream failure as readable text', async () => {
    client.getVideoStatus.mockResolvedValue({
      status: 'failed',
      error: { message: 'encoder crashed' },
    })
    seedVideoMessage()

    renderHook(() => useChatSessions())

    await waitFor(() => {
      expect(activeMessage()?.videoPhase).toBe('failed')
    })
    const message = activeMessage()
    expect(message?.error).toBe(true)
    expect(message?.content).toContain('encoder crashed')
    expect(message?.intent).toBeNull()
  })

  it('does not re-poll a video that already has a persisted result', async () => {
    seedVideoMessage({ videoUrl: 'https://cdn.test/cached.mp4', videoPhase: 'succeeded' })

    renderHook(() => useChatSessions())
    await act(async () => { await Promise.resolve() })

    expect(client.getVideoStatus).not.toHaveBeenCalled()
  })
})
