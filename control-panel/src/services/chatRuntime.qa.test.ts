import { afterEach, expect, it, vi } from 'vitest'
import * as api from '@/api/client'
import { clearAllChatPolling, describeDraftFailure, pollDraftUntilSettled } from './chatRuntime'

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

it('shows actionable guidance when ComfyUI stops reporting progress', async () => {
  vi.useFakeTimers()
  const preview = vi.spyOn(api, 'getDraftPreview')
    .mockRejectedValue(new Error('comfyui_progress_stalled'))

  const resultPromise = pollDraftUntilSettled('draft-stalled')
  await vi.advanceTimersByTimeAsync(1_000)
  const result = await resultPromise

  expect(result.kind).toBe('error')
  if (result.kind !== 'error') throw new Error('expected an error result')
  expect(result.message).toContain('长时间没有返回执行进度')
  expect(result.message).toContain('自动取消')
  expect(result.message).toContain('comfyui_progress_stalled')
  expect(preview).toHaveBeenCalledTimes(1)
})

it.each([
  ['comfyui_invalid_reference_image', 'PNG、JPEG 或 WebP'],
  ['comfyui_reference_image_too_large', '不超过 10 MB、1600 万像素'],
  ['comfyui_qwen_image_reference_unsupported', '切换为 SDXL'],
])('explains reference-image failure %s', (code, guidance) => {
  const message = describeDraftFailure(code)

  expect(message).toContain(guidance)
  expect(message).toContain(code)
})
