import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ChatCompletionRequest } from '@/types'

const generationRequest = vi.hoisted(() => ({
  getGenerationRequest: vi.fn(),
}))

vi.mock('./generationRequest', () => generationRequest)

import { normalizeChatMessages, requestChatCompletion } from './consoleChat'

type Messages = ChatCompletionRequest['messages']

afterEach(() => {
  generationRequest.getGenerationRequest.mockReset()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('normalizeChatMessages', () => {
  it('removes one duplicated terminal user text turn', () => {
    const messages = [
      { role: 'assistant', content: 'ready' },
      { role: 'user', content: '生成一只柯基视频' },
      { role: 'user', content: '生成一只柯基视频' },
    ] as Messages

    const normalized = normalizeChatMessages(messages)

    expect(normalized).toHaveLength(2)
    expect(normalized.filter(message => message.role === 'user')).toHaveLength(1)
    expect(messages).toHaveLength(3)
  })

  it('deduplicates the current reference image together with its text', () => {
    const multimodal = [
      { type: 'text', text: '让这张图动起来' },
      {
        type: 'image_url',
        image_url: { url: 'data:image/png;base64,a2V5ZnJhbWU=' },
      },
    ]
    const messages = [
      { role: 'user', content: multimodal },
      { role: 'user', content: structuredClone(multimodal) },
    ] as Messages

    const normalized = normalizeChatMessages(messages)

    expect(normalized).toHaveLength(1)
    const content = normalized[0]?.content
    expect(Array.isArray(content)).toBe(true)
    const imageParts = Array.isArray(content)
      ? content.filter(part => part.type === 'image_url')
      : []
    expect(imageParts).toHaveLength(1)
  })

  it('keeps deliberate repeated prompts separated by an assistant response', () => {
    const messages = [
      { role: 'user', content: '再试一次' },
      { role: 'assistant', content: '已完成' },
      { role: 'user', content: '再试一次' },
    ] as Messages

    expect(normalizeChatMessages(messages)).toEqual(messages)
  })
})

describe('requestChatCompletion response-loss recovery', () => {
  it('recovers the server-created draft after a non-abort transport failure', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    generationRequest.getGenerationRequest.mockResolvedValue({
      request_id: 'request-1',
      draft_id: 'draft-1',
      status: 'running',
      stage: 'running',
      progress: 0.2,
      media_type: 'video',
      preview_url: '/admin/draft/draft-1/preview',
      workflow_version: 'wan22-v1',
    })

    await expect(requestChatCompletion(
      {
        model: 'auto',
        messages: [{ role: 'user', content: '生成一条龙的视频' }],
        stream: true,
        chat_session_id: 'session-1',
        generation_options: { backend: 'local' },
      },
      undefined,
      'request-1',
    )).resolves.toEqual({
      kind: 'draft',
      draftId: 'draft-1',
      previewUrl: '/admin/draft/draft-1/preview',
      mediaType: 'video',
      generationParams: {
        request_id: 'request-1',
        workflow_version: 'wan22-v1',
      },
    })

    expect(generationRequest.getGenerationRequest).toHaveBeenCalledWith(
      'request-1',
      'session-1',
    )
  })

  it('does not recover after an explicit AbortSignal cancellation', async () => {
    const controller = new AbortController()
    controller.abort()
    const abortError = new DOMException('Aborted', 'AbortError')
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(abortError))

    await expect(requestChatCompletion(
      {
        model: 'auto',
        messages: [{ role: 'user', content: '生成图片' }],
        stream: true,
        chat_session_id: 'session-1',
        generation_options: { backend: 'local' },
      },
      controller.signal,
      'request-1',
    )).rejects.toBe(abortError)

    expect(generationRequest.getGenerationRequest).not.toHaveBeenCalled()
  })
})
