import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useChatStore } from '@/stores/chatStore'
import { useSourceDraftVideo } from './useSourceDraftVideo'

const api = vi.hoisted(() => ({
  createVideoDraftFromSource: vi.fn(),
}))

vi.mock('@/api/sourceDraftVideo', () => api)

const input = {
  sourceDraftId: 'source-image',
  motionPrompt: '柯基跑向镜头',
  durationSeconds: 5 as const,
  fps: 8,
  chatSessionId: 'session-1',
}

beforeEach(() => {
  api.createVideoDraftFromSource.mockReset()
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

describe('useSourceDraftVideo terminal states', () => {
  it('turns pre-draft cancellation into a terminal text message', async () => {
    api.createVideoDraftFromSource.mockImplementation(
      (_draftId, _request, signal: AbortSignal) => new Promise((_resolve, reject) => {
        signal.addEventListener('abort', () => {
          reject(new DOMException('Aborted', 'AbortError'))
        }, { once: true })
      }),
    )
    const { result } = renderHook(() => useSourceDraftVideo())

    let creation: Promise<void>
    act(() => {
      creation = result.current.create(input)
    })
    act(() => {
      result.current.cancel()
    })
    await act(async () => {
      await creation
    })

    const state = useChatStore.getState()
    const assistant = state.sessions[0].messages.at(-1)
    expect(assistant).toMatchObject({
      role: 'assistant',
      content: '已停止',
      intent: null,
      error: false,
      incomplete: false,
      awaitingDraft: false,
    })
    expect(assistant?.draft).toBeUndefined()
    expect(state.streaming).toBe(false)
    expect(state.pendingAssistantId).toBeNull()
  })

  it('renders API failures as terminal text instead of video media', async () => {
    api.createVideoDraftFromSource.mockRejectedValue(
      new Error('无权使用该图片草稿。'),
    )
    const { result } = renderHook(() => useSourceDraftVideo())

    await act(async () => {
      await result.current.create(input)
    })

    const state = useChatStore.getState()
    const assistant = state.sessions[0].messages.at(-1)
    expect(assistant).toMatchObject({
      role: 'assistant',
      content: '无权使用该图片草稿。',
      intent: null,
      error: true,
      incomplete: false,
      awaitingDraft: false,
    })
    expect(assistant?.draft).toBeUndefined()
    expect(state.error).toBe('无权使用该图片草稿。')
    expect(state.streaming).toBe(false)
  })
})
