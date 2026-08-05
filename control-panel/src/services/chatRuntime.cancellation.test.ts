import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  getDraftPreview: vi.fn(),
  getDraftStatus: vi.fn(),
  getVideoStatus: vi.fn(),
}))

vi.mock('@/api/client', () => api)

beforeEach(() => {
  vi.useFakeTimers()
  api.getDraftPreview.mockReset()
  api.getDraftStatus.mockReset()
  api.getVideoStatus.mockReset()
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('draft preview polling cancellation', () => {
  it('does not start polling when already cancelled', async () => {
    const { pollDraftUntilSettled } = await import('./chatRuntime')
    const controller = new AbortController()
    controller.abort()

    await expect(pollDraftUntilSettled(
      'draft-1',
      undefined,
      controller.signal,
    )).resolves.toEqual({ kind: 'cancelled', message: '已停止' })
    expect(api.getDraftPreview).not.toHaveBeenCalled()
  })

  it('cancels during the polling delay without issuing a request', async () => {
    const { pollDraftUntilSettled } = await import('./chatRuntime')
    const controller = new AbortController()
    const polling = pollDraftUntilSettled(
      'draft-2',
      undefined,
      controller.signal,
    )

    controller.abort()
    await vi.runAllTimersAsync()

    await expect(polling).resolves.toEqual({
      kind: 'cancelled',
      message: '已停止',
    })
    expect(api.getDraftPreview).not.toHaveBeenCalled()
  })

  it('ignores a preview response that arrives after cancellation', async () => {
    let resolvePreview: ((value: { previewDataUrl: string }) => void) | undefined
    api.getDraftPreview.mockImplementation(() => new Promise(resolve => {
      resolvePreview = resolve
    }))
    const { pollDraftUntilSettled } = await import('./chatRuntime')
    const controller = new AbortController()
    const onProgress = vi.fn()
    const polling = pollDraftUntilSettled(
      'draft-3',
      onProgress,
      controller.signal,
    )

    await vi.advanceTimersByTimeAsync(1_000)
    expect(api.getDraftPreview).toHaveBeenCalledWith('draft-3')
    controller.abort()
    resolvePreview?.({ previewDataUrl: 'data:image/png;base64,cHJldmlldw==' })

    await expect(polling).resolves.toEqual({
      kind: 'cancelled',
      message: '已停止',
    })
    expect(onProgress).not.toHaveBeenCalled()
  })
})
