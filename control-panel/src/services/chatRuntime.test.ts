import { describe, expect, it } from 'vitest'
import { consumeChatEventStream, type ChatStreamChunk } from './chatRuntime'

describe('consumeChatEventStream', () => {
  it('decodes split SSE frames and stops at DONE', async () => {
    const encoder = new TextEncoder()
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('data: {"choices":[{"delta":{"content":"你"}}]}\n'))
        controller.enqueue(encoder.encode('\ndata: not-json\n\ndata: {"choices":[{"delta":{"content":"好"}}]}\n\n'))
        controller.enqueue(encoder.encode('data: [DONE]\n\ndata: {"error":{"message":"ignored"}}\n\n'))
        controller.close()
      },
    })
    const chunks: ChatStreamChunk[] = []

    await consumeChatEventStream(stream, chunk => chunks.push(chunk))

    expect(chunks.map(chunk => chunk.choices?.[0]?.delta?.content)).toEqual(['你', '好'])
  })
})
