import { getDraftPreview, getDraftStatus, getVideoStatus } from '@/api/client'
import type { ChatPageMessage, VideoStatusResponse } from '@/types'

let messageIdCounter = 0

export const resumedSessionIds = new Set<string>()
export const pollingDraftIds = new Set<string>()

export const VIDEO_POLL_INTERVAL_MS = 5_000
export const VIDEO_POLL_MAX_ATTEMPTS = 360
export const DRAFT_POLL_INTERVAL_MS = 1_000
export const DRAFT_POLL_MAX_ATTEMPTS = 1_260

/**
 * 视频轮询的总预算。任何等待视频结果的 UI 都必须使用这个上限，
 * 不要各自定义超时：组件超时短于轮询预算时，后端仍在生成的任务会被
 * 前端提前判成超时，成品永远不显示。
 */
export const VIDEO_POLL_TIMEOUT_MS = VIDEO_POLL_INTERVAL_MS * VIDEO_POLL_MAX_ATTEMPTS

export function nextMessageId(): string {
  messageIdCounter += 1
  return `msg-${Date.now()}-${messageIdCounter}`
}

export function newSessionId(): string {
  return `sess-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

export function clearAllChatPolling(): void {
  // 只清去重集合不会停止已在运行的循环：那些循环会继续打后端，并且下一次
  // 挂载时去重记录已消失，同一个 id 会再起一条循环并不断累积。必须真正中止。
  for (const draftAbort of draftPollAborts.values()) draftAbort.abort()
  draftPollAborts.clear()
  pollingDraftIds.clear()
  for (const poll of videoPolls.values()) poll.controller.abort()
  videoPolls.clear()
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

const draftPollAborts = new Map<string, AbortController>()

/**
 * 注册一个可被 abortDraftPoll / clearAllChatPolling 中止的草稿轮询。
 *
 * 调用方不传 signal 时循环会跑满 DRAFT_POLL_MAX_ATTEMPTS(约 21 分钟)，
 * 停止按钮只能取消服务端草稿，前端仍在空转轮询。
 */
export function registerDraftPoll(draftId: string): AbortSignal {
  draftPollAborts.get(draftId)?.abort()
  const controller = new AbortController()
  draftPollAborts.set(draftId, controller)
  return controller.signal
}

export function abortDraftPoll(draftId: string): void {
  const controller = draftPollAborts.get(draftId)
  if (!controller) return
  draftPollAborts.delete(draftId)
  controller.abort()
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
    draftPollAborts.delete(draftId)
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

export function isVideoSucceeded(status: VideoStatusResponse): boolean {
  return status.status === 'succeeded' || status.status === 'completed'
}

/**
 * 从视频状态响应中提取可播放 URL。
 *
 * 三种位置都要尝试：Agnes 把成品 URL 放在 metadata.url，顶层既没有 url 也没有
 * video.url。任何只看其中一两个位置的调用方都会把已完成的任务当成"还没出结果"，
 * 于是既不显示视频也不报错。这是唯一的提取实现，不要在调用方复制。
 */
export function extractVideoUrl(status: VideoStatusResponse): string | null {
  return status.video?.url || status.url || status.metadata?.url || null
}

/** 只接受 data: 与 http(s):// ，阻断 javascript: 等危险协议。 */
export function isPlayableVideoUrl(url: string): boolean {
  return url.startsWith('data:') || /^https?:\/\//i.test(url)
}

export type VideoPollOutcome =
  | { kind: 'terminal'; status: VideoStatusResponse }
  | { kind: 'timeout' }
  | { kind: 'cancelled' }

interface VideoPollHandle {
  result: Promise<VideoPollOutcome>
  controller: AbortController
  subscribers: number
}

const videoPolls = new Map<string, VideoPollHandle>()

export interface VideoWatch {
  result: Promise<VideoPollOutcome>
  release: () => void
}

function waitBeforeNextVideoPoll(signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.resolve()
  return new Promise(resolve => {
    const timer = setTimeout(done, VIDEO_POLL_INTERVAL_MS)
    function done() {
      clearTimeout(timer)
      signal.removeEventListener('abort', done)
      resolve()
    }
    signal.addEventListener('abort', done, { once: true })
  })
}

async function runVideoPoll(
  videoId: string,
  signal: AbortSignal,
): Promise<VideoPollOutcome> {
  for (let attempt = 0; attempt < VIDEO_POLL_MAX_ATTEMPTS; attempt += 1) {
    if (signal.aborted) return { kind: 'cancelled' }
    // 先查一次再等待。先等待会在提交后凭空插入一个轮询间隔的空窗，
    // 对已经完成的任务（例如刷新后恢复）尤其浪费。
    try {
      const status = await getVideoStatus(videoId)
      if (signal.aborted) return { kind: 'cancelled' }
      if (isVideoTerminal(status.status)) return { kind: 'terminal', status }
    } catch (error) {
      if (signal.aborted) return { kind: 'cancelled' }
      console.warn(`Failed to poll video status for ${videoId}:`, error)
    }
    await waitBeforeNextVideoPoll(signal)
  }
  return signal.aborted ? { kind: 'cancelled' } : { kind: 'timeout' }
}

/**
 * 订阅某个视频任务的终态，同一 id 全局只跑一条轮询循环。
 *
 * 之前消息状态层和渲染组件各自独立轮询同一个 id：请求量翻倍，两边的状态还会
 * 分叉（一边解析出了 URL，另一边超时）。这里用引用计数做扇出，所有订阅者共享
 * 同一次循环与同一个结果，最后一个订阅者释放时才真正中止。
 */
export function watchVideoUntilTerminal(videoId: string): VideoWatch {
  let handle = videoPolls.get(videoId)
  if (!handle) {
    const controller = new AbortController()
    const created: VideoPollHandle = {
      controller,
      subscribers: 0,
      result: runVideoPoll(videoId, controller.signal).finally(() => {
        if (videoPolls.get(videoId) === created) videoPolls.delete(videoId)
      }),
    }
    videoPolls.set(videoId, created)
    handle = created
  }
  const active = handle
  active.subscribers += 1
  let released = false
  return {
    result: active.result,
    release: () => {
      if (released) return
      released = true
      active.subscribers -= 1
      if (active.subscribers <= 0) {
        active.controller.abort()
        if (videoPolls.get(videoId) === active) videoPolls.delete(videoId)
      }
    },
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
