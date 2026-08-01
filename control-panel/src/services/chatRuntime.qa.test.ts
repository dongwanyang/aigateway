import { afterEach, expect, it, vi } from 'vitest'
import * as api from '@/api/client'
import { clearAllChatPolling, pollDraftUntilSettled } from './chatRuntime'

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
  clearAllChatPolling()
})

it('shows actionable guidance while retaining the ComfyUI OOM code', async () => {
  vi.useFakeTimers()
  const preview = vi.spyOn(api, 'getDraftPreview')
    .mockRejectedValue(new Error('comfyui_gpu_out_of_memory'))

  const resultPromise = pollDraftUntilSettled('draft-oom')
  await vi.advanceTimersByTimeAsync(1_000)
  const result = await resultPromise

  expect(result.kind).toBe('error')
  if (result.kind !== 'error') throw new Error('expected an error result')
  expect(result.message).toContain('显存不足')
  expect(result.message).toContain('降低分辨率')
  expect(result.message).toContain('comfyui_gpu_out_of_memory')
  expect(preview).toHaveBeenCalledTimes(1)
})
