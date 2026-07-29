import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from '@/api/client'
import {
  clearAllChatPolling,
  consumeChatEventStream,
  pollDraftUntilSettled,
  type ChatStreamChunk,
} from './chatRuntime'

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
  clearAllChatPolling()
})

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

  it('stops polling immediately when draft access is forbidden', async () => {
    vi.useFakeTimers()
    const preview = vi.spyOn(api, 'getDraftPreview').mockRejectedValue(new Error('forbidden'))

    const resultPromise = pollDraftUntilSettled('draft-forbidden')
    await vi.advanceTimersByTimeAsync(1_000)

    await expect(resultPromise).resolves.toEqual({ kind: 'error', message: 'forbidden' })
    expect(preview).toHaveBeenCalledTimes(1)
  })

  it('keeps polling through 202 running/queued/refining states until preview is ready', async () => {
    vi.useFakeTimers()
    // Backend (admin_routes.py) returns 202 with status in
    // {generating, queued, running, refining} while the draft is still being
    // produced, then 200 with preview_data_url once ready. The frontend must
    // keep polling across ALL in-progress statuses, not just 'generating'.
    const preview = vi.spyOn(api, 'getDraftPreview')
      .mockResolvedValueOnce({ status: 'running' })
      .mockResolvedValueOnce({ status: 'queued' })
      .mockResolvedValueOnce({ status: 'refining' })
      .mockResolvedValueOnce({ previewDataUrl: 'data:image/png;base64,abc', previewCount: 1 })

    const resultPromise = pollDraftUntilSettled('draft-running')
    await vi.advanceTimersByTimeAsync(4_000)

    await expect(resultPromise).resolves.toEqual({ kind: 'ready', previewDataUrl: 'data:image/png;base64,abc' })
    expect(preview).toHaveBeenCalledTimes(4)
  })
})
