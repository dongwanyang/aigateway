import { describe, expect, it } from 'vitest'
import type { ChatCompletionRequest } from '@/types'
import { normalizeChatMessages } from './consoleChat'

type Messages = ChatCompletionRequest['messages']

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
    const serialized = JSON.stringify(normalized)
    expect(serialized.match(/image_url/g)).toHaveLength(1)
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
