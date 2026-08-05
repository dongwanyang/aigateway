import { beforeEach, describe, expect, it } from 'vitest'
import { useChatStore } from './chatStore'
import type { ChatSession } from '@/types'

function cancelledSession(): ChatSession {
  return {
    id: 'session-1',
    title: '测试',
    createdAt: 1,
    updatedAt: 1,
    messages: [
      {
        id: 'assistant-1',
        role: 'assistant',
        content: '已停止',
        generationRequestId: 'request-1',
        awaitingDraft: false,
        ts: 1,
        draft: {
          draftId: 'draft-1',
          previewUrl: '/admin/draft/draft-1/preview',
          mediaType: 'video',
          status: 'cancelled',
          stage: 'cancelled',
          progress: 0,
          errorMessage: '已停止',
        },
      },
    ],
  }
}

beforeEach(() => {
  useChatStore.setState({
    sessions: [cancelledSession()],
    activeId: 'session-1',
    streaming: false,
    error: null,
    pendingAssistantId: null,
    resumePollingKey: 0,
  })
})

describe('chat store terminal-state protection', () => {
  it('does not let a late poll overwrite cancelled for the same draft', () => {
    useChatStore.getState().setSessions(previous => previous.map(session => ({
      ...session,
      messages: session.messages.map(message => ({
        ...message,
        intent: 'generation:video',
        model: 'comfyui',
        error: true,
        awaitingDraft: true,
        draft: message.draft ? {
          ...message.draft,
          status: 'pending',
          stage: 'preview_ready',
          progress: 1,
          errorMessage: 'late write',
        } : undefined,
      })),
    })))

    const message = useChatStore.getState().sessions[0].messages[0]
    expect(message.draft).toMatchObject({
      draftId: 'draft-1',
      status: 'cancelled',
      stage: 'cancelled',
      progress: 0,
    })
    expect(message.awaitingDraft).toBe(false)
    expect(message.intent).toBeNull()
    expect(message.error).toBe(false)
  })

  it('allows a deliberate replacement with a new draft identity', () => {
    useChatStore.getState().setSessions(previous => previous.map(session => ({
      ...session,
      messages: session.messages.map(message => ({
        ...message,
        awaitingDraft: true,
        draft: message.draft ? {
          ...message.draft,
          draftId: 'draft-2',
          status: 'generating',
          stage: 'queued',
        } : undefined,
      })),
    })))

    const message = useChatStore.getState().sessions[0].messages[0]
    expect(message.draft).toMatchObject({
      draftId: 'draft-2',
      status: 'generating',
      stage: 'queued',
    })
    expect(message.awaitingDraft).toBe(true)
  })
})
