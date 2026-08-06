import { act, renderHook } from '@testing-library/react'
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
  getDraftPreview: vi.fn(),
  getDraftStatus: vi.fn(),
}))

vi.mock('@/api/consoleChat', () => consoleChat)
vi.mock('@/api/client', () => client)

function seedSession(messages: ChatPageMessage[] = []) {
  useChatStore.setState({
    sessions: [{
      id: 'session-1',
      title: '新对话',
      messages,
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

beforeEach(() => {
  vi.clearAllMocks()
  client.cancelDraft.mockResolvedValue({ draft_id: 'draft-1', cancelled: true })
  client.getDraftStatus.mockResolvedValue({ status: 'running', progress: 0.1, stage: 'running' })
  localStorage.clear()
  seedSession()
})

describe('draft preview polling is cancellable', () => {
  it('stops polling the preview once the user presses stop', async () => {
    vi.useFakeTimers()
    client.getDraftPreview.mockResolvedValue({ status: 'running', progress: 0.1, stage: 'running' })
    consoleChat.requestChatCompletion.mockResolvedValue({
      kind: 'draft',
      draftId: 'draft-1',
      previewUrl: '/admin/draft/draft-1/preview',
      mediaType: 'image',
      generationParams: {},
    })

    const { result } = renderHook(() => useChatSessions())

    await act(async () => {
      void result.current.send('画一只猫')
      await vi.advanceTimersByTimeAsync(0)
    })

    await act(async () => { await vi.advanceTimersByTimeAsync(3_000) })
    const pollsBeforeStop = client.getDraftPreview.mock.calls.length
    expect(pollsBeforeStop).toBeGreaterThan(0)

    await act(async () => {
      result.current.stop()
      await vi.advanceTimersByTimeAsync(0)
    })

    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(client.getDraftPreview.mock.calls.length).toBe(pollsBeforeStop)
    expect(client.cancelDraft).toHaveBeenCalledWith('draft-1')
    vi.useRealTimers()
  })
})

describe('reference images stay scoped to their own turn', () => {
  it('omits an ephemeral source image from the outgoing conversation history', async () => {
    consoleChat.requestChatCompletion.mockResolvedValue({
      kind: 'stream',
      body: new ReadableStream({
        start(controller) {
          controller.enqueue(new TextEncoder().encode('data: [DONE]\n\n'))
          controller.close()
        },
      }),
    })

    seedSession([
      {
        id: 'user-video',
        role: 'user',
        content: '让它动起来',
        referenceImageDataUrl: 'data:image/png;base64,SOURCEPREVIEW',
        referenceImageName: '已生成图片',
        referenceImageEphemeral: true,
        ts: 1,
      },
      {
        id: 'user-upload',
        role: 'user',
        content: '照这张改一下',
        referenceImageDataUrl: 'data:image/png;base64,USERUPLOAD',
        referenceImageName: 'upload.png',
        ts: 2,
      },
    ])

    const { result } = renderHook(() => useChatSessions())
    await act(async () => {
      await result.current.send('讲个笑话')
    })

    const body = consoleChat.requestChatCompletion.mock.calls[0][0]
    const serialized = JSON.stringify(body.messages)
    expect(serialized).not.toContain('SOURCEPREVIEW')
    expect(serialized).toContain('USERUPLOAD')
  })
})
