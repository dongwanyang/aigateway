import { getDraftPreview, getDraftStatus, getVideoStatus } from '@/api/client'
import type { ChatPageMessage, VideoStatusResponse } from '@/types'

let messageIdCounter = 0

export const resumedSessionIds = new Set<string>()
export const pollingVideoIds = new Set<string>()
export const pollingDraftIds = new Set<string>()

export const VIDEO_POLL_INTERVAL_MS = 5_000
export const VIDEO_POLL_MAX_ATTEMPTS = 360
export const DRAFT_POLL_INTERVAL_MS = 1_000
export const DRAFT_POLL_MAX_ATTEMPTS = 1_260

export function nextMessageId(): string {
  messageIdCounter += 1
  return `msg-${Date.now()}-${messageIdCounter}`
}

export function newSessionId(): string {
  return `sess-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

export function clearAllChatPolling(): void {
  pollingVideoIds.clear()
  pollingDraftIds.clear()
}

export function wait(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

export type DraftPollResult =
  | { kind: 'duplicate' }
  | { kind: 'ready'; previewDataUrl: string }
  | { kind: 'expired'; message: string }
  | { kind: 'cancelled'; message: string }
  | { kind: 'error'; message: string }

export function describeDraftFailure(message: string): string {
  const normalized = message.toLowerCase()
  if (normalized.includes('comfyui_gpu_out_of_memory')) {
    return 'ComfyUI 显存不足，无法完成当前图片工作流。请降低分辨率或批量大小，释放显存后重试。（comfyui_gpu_out_of_memory）'
  }
  if (normalized.includes('comfyui_recovery_failed')) {
    return 'ComfyUI 任务已结束，但结果恢复失败。请重试；若持续发生，请检查 ComfyUI 历史记录。（comfyui_recovery_failed）'
  }
  if (normalized.includes('comfyui_progress_stalled')) {
    return 'ComfyUI 长时间没有返回执行进度，任务已自动取消。请检查 ComfyUI 日志或 GPU 状态后重试。（comfyui_progress_stalled）'
  }
  if (normalized.includes('comfyui_invalid_reference_image')) {
    return '参考图片无效或格式不受支持。请重新选择 PNG、JPEG 或 WebP 图片后重试。（comfyui_invalid_reference_image）'
  }
  if (normalized.includes('comfyui_reference_image_too_large')) {
    return '参考图片尺寸过大。请使用不超过 10 MB、1600 万像素的图片后重试。（comfyui_reference_image_too_large）'
  }
  if (normalized.includes('comfyui_qwen_image_reference_unsupported')) {
    return '当前 Qwen 图片工作流暂不支持参考图。请将图片模型预设切换为 SDXL 后重试。（comfyui_qwen_image_reference_unsupported）'
  }
  if (normalized.includes('draft_cancelled')) {
    return '生成已取消。'
  }
  if (normalized.includes('reference_image_required')) {
    return '未找到参考图片，请上传图片或从图片结果点击“基于此图生成视频”。（reference_image_required）'
  }
  return message
}

export interface DraftPollProgress {
  status?: string
  stage?: string
  progress?: number
  progressSource?: string
}

function waitUntilNextPoll(signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.resolve()
  return new Promise(resolve => {
    const timer = setTimeout(done, DRAFT_POLL_INTERVAL_MS)
    function done() {
      clearTimeout(timer)
      signal.removeEventListener('abort', done)
      resolve()
    }
    signal.addEventListener('abort', done, { once: true })
  })
}

export async function pollDraftUntilSettled(
  draftId: string,
  onProgress?: (progress: DraftPollProgress) => void,
  signal?: AbortSignal,
): Promise<DraftPollResult> {
  if (pollingDraftIds.has(draftId)) return { kind: 'duplicate' }
  if (signal?.aborted) return { kind: 'cancelled', message: '已停止' }
  pollingDraftIds.add(draftId)
  try {
    for (let attempt = 0; attempt < DRAFT_POLL_MAX_ATTEMPTS; attempt += 1) {
      if (signal) await waitUntilNextPoll(signal)
      else await wait(DRAFT_POLL_INTERVAL_MS)
      if (signal?.aborted) return { kind: 'cancelled', message: '已停止' }
      try {
        const response = await getDraftPreview(draftId)
        if (signal?.aborted) return { kind: 'cancelled', message: '已停止' }
        if (!response.previewDataUrl) {
          onProgress?.({
            status: response.status ?? 'running',
            stage: response.stage,
            progress: response.progress,
            progressSource: response.progressSource,
          })
          continue
        }
        onProgress?.({
          status: 'pending',
          stage: 'preview_ready',
          progress: 1,
          progressSource: 'complete',
        })
        return { kind: 'ready', previewDataUrl: response.previewDataUrl }
      } catch (error) {
        if (signal?.aborted) return { kind: 'cancelled', message: '已停止' }
        const message = error instanceof Error ? error.message : '预览加载失败'
        if (message.includes('not_found') || message.includes('expired')) {
          return { kind: 'expired', message }
        }
        if (
          message.includes('draft_failed')
          || message.includes('draft_worker_lost')
          || message.includes('comfyui_job_lost')
          || message.includes('comfyui_recovery_failed')
          || message.includes('comfyui_')
          || message.includes('draft_cancelled')
        ) {
          return { kind: 'error', message: describeDraftFailure(message) }
        }
        if (message.includes('forbidden') || message.includes('unauthorized')) {
          return { kind: 'error', message }
        }
        console.warn(`Failed to poll draft preview for ${draftId}:`, error)
      }
    }
    return { kind: 'expired', message: '草稿生成超时' }
  } finally {
    pollingDraftIds.delete(draftId)
  }
}

/** Poll status while the blocking confirmation request performs local refine. */
export async function pollDraftProgressUntilStopped(
  draftId: string,
  signal: AbortSignal,
  onProgress: (progress: DraftPollProgress) => void,
): Promise<void> {
  let confirmationStarted = false
  for (let attempt = 0; attempt < DRAFT_POLL_MAX_ATTEMPTS && !signal.aborted; attempt += 1) {
    await waitUntilNextPoll(signal)
    if (signal.aborted) return
    try {
      const status = await getDraftStatus(draftId)
      if (status.status === 'refining' || status.status === 'confirming') {
        confirmationStarted = true
      }
      onProgress({
        status: status.status,
        stage: status.stage,
        progress: status.progress,
        progressSource: status.progressSource,
      })
      if (
        ['completed', 'confirmed', 'failed', 'cancelled', 'expired'].includes(status.status)
        || (confirmationStarted && status.status === 'pending')
      ) return
    } catch (error) {
      if (signal.aborted) return
      console.warn(`Failed to poll draft confirmation progress for ${draftId}:`, error)
    }
  }
}

function isVideoTerminal(status: string | undefined): boolean {
  return ['succeeded', 'completed', 'failed', 'error', 'expired'].includes(status ?? '')
}

export async function pollVideoUntilTerminal(videoId: string): Promise<VideoStatusResponse | null> {
  if (pollingVideoIds.has(videoId)) return null
  pollingVideoIds.add(videoId)
  try {
    for (let attempt = 0; attempt < VIDEO_POLL_MAX_ATTEMPTS; attempt += 1) {
      await wait(VIDEO_POLL_INTERVAL_MS)
      try {
        const status = await getVideoStatus(videoId)
        if (isVideoTerminal(status.status)) return status
      } catch (error) {
        console.warn(`Failed to poll video status for ${videoId}:`, error)
      }
    }
    return null
  } finally {
    pollingVideoIds.delete(videoId)
  }
}

export interface ChatStreamChunk {
  choices?: Array<{ delta?: { content?: string } }>
  _meta?: {
    routed_to?: { intent?: ChatPageMessage['intent']; model?: string }
    video_id?: string
  }
  error?: { message?: string; code?: string }
}

export async function consumeChatEventStream(
  stream: ReadableStream<Uint8Array>,
  onChunk: (chunk: ChatStreamChunk) => void,
): Promise<void> {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) return
      buffer += decoder.decode(value, { stream: true })
      let boundary = buffer.indexOf('\n\n')
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary).trim()
        buffer = buffer.slice(boundary + 2)
        boundary = buffer.indexOf('\n\n')
        if (!frame.startsWith('data:')) continue
        const payload = frame.slice(5).trim()
        if (payload === '[DONE]') return
        try {
          onChunk(JSON.parse(payload) as ChatStreamChunk)
        } catch {
          // Ignore malformed/non-JSON frames without terminating the stream.
        }
      }
    }
  } finally {
    try {
      reader.releaseLock()
    } catch {
      // The stream may already have released its reader after an error.
    }
  }
}
