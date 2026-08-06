import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useChatStore } from '@/stores/chatStore'
import { useChatSessions } from './useChatSessions'

const consoleChat = vi.hoisted(() => ({
  requestChatCompletion: vi.fn(),
}))
const client = vi.hoisted(() => ({
  cancelDraft: vi.fn(),
  getDraftResult: vi.fn(),
  confirmDraft: vi.fn(),
  rejectDraft: vi.fn(),
  deleteSessionDrafts: vi.fn(),
}))
const runtime = vi.hoisted(() => ({
  pollDraftUntilSettled: vi.fn(),
}))

vi.mock('@/api/consoleChat', () => consoleChat)
vi.mock('@/api/client', () => client)
vi.mock('@/services/chatRuntime', async () => {
  const actual = await vi.importActual<
    typeof import('@/services/chatRuntime')
  >('@/services/chatRuntime')
  return { ...actual, ...runtime }
})

beforeEach(() => {
  vi.clearAllMocks()
  client.cancelDraft.mockResolvedValue({ draft_id: 'd1', cancelled: true })
  runtime.pollDraftUntilSettled.mockResolvedValue({ kind: 'duplicate' })
  useChatStore.setState({
    sessions: [{
      id: 'session-1',
      title: '新对话',
      messages: [],
      createdAt: 1,
      updatedAt: 1,
    }],
    activeId: 'session-1',
    streaming: false,
    error: null,
    pendingAssistantId: null,
    resumePollingKey: 0,
  })
})

describe('stopping a draft generation releases the server-side work', () => {
  it('cancels the draft that arrives after the user pressed stop', async () => {
    // Regression: stop() used to abort the fetch before the response was read,
    // so the client never learned the draft id and the backend kept generating.
    // The orphan then starved the drafts the UI was still polling.
    let release: (value: unknown) => void = () => {}
    consoleChat.requestChatCompletion.mockImplementation(
      () => new Promise(resolve => { release = resolve }),
    )

    const { result } = renderHook(() => useChatSessions())

    await act(async () => {
      void result.current.send('画一条龙')
      await Promise.resolve()
    })

    await act(async () => {
      result.current.stop()
      await Promise.resolve()
    })

    // The request must still be allowed to land, otherwise the draft id is lost.
    expect(consoleChat.requestChatCompletion).toHaveBeenCalledTimes(1)

    await act(async () => {
      release({
        kind: 'draft',
        draftId: 'draft-dragon',
        previewUrl: '/admin/draft/draft-dragon/preview',
        mediaType: 'image',
        generationParams: {},
      })
      await Promise.resolve()
    })

    await waitFor(() => {
      expect(client.cancelDraft).toHaveBeenCalledWith('draft-dragon')
    })
    // A cancelled draft must not be polled.
    expect(runtime.pollDraftUntilSettled).not.toHaveBeenCalled()
  })

  it('cancels an already-tracked draft when stop is pressed while polling', async () => {
    consoleChat.requestChatCompletion.mockResolvedValue({
      kind: 'draft',
      draftId: 'draft-pig',
      previewUrl: '/admin/draft/draft-pig/preview',
      mediaType: 'image',
      generationParams: {},
    })
    runtime.pollDraftUntilSettled.mockImplementation(
      () => new Promise(() => {}),
    )

    const { result } = renderHook(() => useChatSessions())

    await act(async () => {
      await result.current.send('画一只猪')
    })

    await waitFor(() => {
      expect(runtime.pollDraftUntilSettled).toHaveBeenCalled()
    })

    await act(async () => {
      result.current.stop()
      await Promise.resolve()
    })

    expect(client.cancelDraft).toHaveBeenCalledWith('draft-pig')
  })

  it('does not cancel anything when a text stream is stopped', async () => {
    consoleChat.requestChatCompletion.mockResolvedValue({
      kind: 'stream',
      body: new ReadableStream({ start(controller) { controller.close() } }),
    })

    const { result } = renderHook(() => useChatSessions())

    await act(async () => {
      await result.current.send('你好')
    })
    await act(async () => {
      result.current.stop()
      await Promise.resolve()
    })

    expect(client.cancelDraft).not.toHaveBeenCalled()
  })
})
